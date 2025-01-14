# Copyright 2013-2016 Camptocamp SA
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl.html)

import ast
import logging
from collections import namedtuple
from datetime import datetime, timedelta

from odoo import _, api, exceptions, fields, models, tools
from odoo.osv import expression

from ..fields import JobSerialized
from ..job import DONE, PENDING, STATES, Job, job_function_name

# TODO deprecated by :job-no-decorator:
channel_func_name = job_function_name


_logger = logging.getLogger(__name__)


class QueueJob(models.Model):
    """Model storing the jobs to be executed."""

    _name = "queue.job"
    _description = "Queue Job"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _log_access = False

    _order = "date_created DESC, date_done DESC"

    _removal_interval = 30  # days
    _default_related_action = "related_action_open_record"

    uuid = fields.Char(string="UUID", readonly=True, index=True, required=True)
    user_id = fields.Many2one(comodel_name="res.users", string="User ID", required=True)
    company_id = fields.Many2one(
        comodel_name="res.company", string="Company", index=True
    )
    name = fields.Char(string="Description", readonly=True)

    model_name = fields.Char(string="Model", readonly=True)
    method_name = fields.Char(readonly=True)
    record_ids = JobSerialized(readonly=True, base_type=list)
    args = JobSerialized(readonly=True, base_type=tuple)
    kwargs = JobSerialized(readonly=True, base_type=dict)
    func_string = fields.Char(
        string="Task", compute="_compute_func_string", readonly=True, store=True
    )

    state = fields.Selection(STATES, readonly=True, required=True, index=True)
    priority = fields.Integer()
    exc_info = fields.Text(string="Exception Info", readonly=True)
    result = fields.Text(readonly=True)

    date_created = fields.Datetime(string="Created Date", readonly=True)
    date_started = fields.Datetime(string="Start Date", readonly=True)
    date_enqueued = fields.Datetime(string="Enqueue Time", readonly=True)
    date_done = fields.Datetime(readonly=True)

    eta = fields.Datetime(string="Execute only after")
    retry = fields.Integer(string="Current try")
    max_retries = fields.Integer(
        string="Max. retries",
        help="The job will fail if the number of tries reach the "
        "max. retries.\n"
        "Retries are infinite when empty.",
    )
    channel_method_name = fields.Char(
        readonly=True, compute="_compute_job_function", store=True
    )
    job_function_id = fields.Many2one(
        comodel_name="queue.job.function",
        compute="_compute_job_function",
        string="Job Function",
        readonly=True,
        store=True,
    )

    override_channel = fields.Char()
    channel = fields.Char(
        compute="_compute_channel", inverse="_inverse_channel", store=True, index=True
    )

    identity_key = fields.Char()

    def init(self):
        self._cr.execute(
            "SELECT indexname FROM pg_indexes WHERE indexname = %s ",
            ("queue_job_identity_key_state_partial_index",),
        )
        if not self._cr.fetchone():
            self._cr.execute(
                "CREATE INDEX queue_job_identity_key_state_partial_index "
                "ON queue_job (identity_key) WHERE state in ('pending', "
                "'enqueued') AND identity_key IS NOT NULL;"
            )

    def _inverse_channel(self):
        for record in self:
            record.override_channel = record.channel

    @api.depends("job_function_id.channel_id")
    def _compute_channel(self):
        for record in self:
            channel = (
                record.override_channel or record.job_function_id.channel or "root"
            )
            if record.channel != channel:
                record.channel = channel

    @api.depends("model_name", "method_name", "job_function_id.channel_id")
    def _compute_job_function(self):
        for record in self:
            model = self.env[record.model_name]
            method = getattr(model, record.method_name)
            channel_method_name = job_function_name(model, method)
            func_model = self.env["queue.job.function"]
            function = func_model.search([("name", "=", channel_method_name)], limit=1)
            record.channel_method_name = channel_method_name
            record.job_function_id = function

    @api.depends("model_name", "method_name", "record_ids", "args", "kwargs")
    def _compute_func_string(self):
        for record in self:
            record_ids = record.record_ids
            model = repr(self.env[record.model_name].browse(record_ids))
            args = [repr(arg) for arg in record.args]
            kwargs = ["{}={!r}".format(key, val) for key, val in record.kwargs.items()]
            all_args = ", ".join(args + kwargs)
            record.func_string = "{}.{}({})".format(model, record.method_name, all_args)

    def open_related_action(self):
        """Open the related action associated to the job"""
        self.ensure_one()
        job = Job.load(self.env, self.uuid)
        action = job.related_action()
        if action is None:
            raise exceptions.UserError(_("No action available for this job"))
        return action

    def _change_job_state(self, state, result=None):
        """Change the state of the `Job` object

        Changing the state of the Job will automatically change some fields
        (date, result, ...).
        """
        for record in self:
            job_ = Job.load(record.env, record.uuid)
            if state == DONE:
                job_.set_done(result=result)
            elif state == PENDING:
                job_.set_pending(result=result)
            else:
                raise ValueError("State not supported: %s" % state)
            job_.store()

    def button_done(self):
        result = _("Manually set to done by %s") % self.env.user.name
        self._change_job_state(DONE, result=result)
        return True

    def requeue(self):
        self._change_job_state(PENDING)
        return True

    def _message_post_on_failure(self):
        # subscribe the users now to avoid to subscribe them
        # at every job creation
        domain = self._subscribe_users_domain()
        users = self.env["res.users"].search(domain)
        self.message_subscribe(partner_ids=users.mapped("partner_id").ids)
        for record in self:
            msg = record._message_failed_job()
            if msg:
                record.message_post(body=msg)

    def write(self, vals):
        res = super(QueueJob, self).write(vals)
        if vals.get("state") == "failed":
            self._message_post_on_failure()
        return res

    def _subscribe_users_domain(self):
        """Subscribe all users having the 'Queue Job Manager' group"""
        group = self.env.ref("queue_job.group_queue_job_manager")
        if not group:
            return None
        companies = self.mapped("company_id")
        domain = [("groups_id", "=", group.id)]
        if companies:
            domain.append(("company_id", "in", companies.ids))
        return domain

    def _message_failed_job(self):
        """Return a message which will be posted on the job when it is failed.

        It can be inherited to allow more precise messages based on the
        exception informations.

        If nothing is returned, no message will be posted.
        """
        self.ensure_one()
        return _(
            "Something bad happened during the execution of the job. "
            "More details in the 'Exception Information' section."
        )

    def _needaction_domain_get(self):
        """Returns the domain to filter records that require an action

        :return: domain or False is no action
        """
        return [("state", "=", "failed")]

    def autovacuum(self):
        """Delete all jobs done based on the removal interval defined on the
           channel

        Called from a cron.
        """
        for channel in self.env["queue.job.channel"].search([]):
            deadline = datetime.now() - timedelta(days=int(channel.removal_interval))
            jobs = self.search(
                [("date_done", "<=", deadline), ("channel", "=", channel.complete_name)]
            )
            if jobs:
                jobs.unlink()
        return True

    def requeue_stuck_jobs(self, enqueued_delta=5, started_delta=0):
        """Fix jobs that are in a bad states

        :param in_queue_delta: lookup time in minutes for jobs
                                that are in enqueued state

        :param started_delta: lookup time in minutes for jobs
                                that are in enqueued state,
                                0 means that it is not checked
        """
        self._get_stuck_jobs_to_requeue(
            enqueued_delta=enqueued_delta, started_delta=started_delta
        ).requeue()
        return True

    def _get_stuck_jobs_domain(self, queue_dl, started_dl):
        domain = []
        now = fields.datetime.now()
        if queue_dl:
            queue_dl = now - timedelta(minutes=queue_dl)
            domain.append(
                [
                    "&",
                    ("date_enqueued", "<=", fields.Datetime.to_string(queue_dl)),
                    ("state", "=", "enqueued"),
                ]
            )
        if started_dl:
            started_dl = now - timedelta(minutes=started_dl)
            domain.append(
                [
                    "&",
                    ("date_started", "<=", fields.Datetime.to_string(started_dl)),
                    ("state", "=", "started"),
                ]
            )
        if not domain:
            raise exceptions.ValidationError(
                _("If both parameters are 0, ALL jobs will be requeued!")
            )
        return expression.OR(domain)

    def _get_stuck_jobs_to_requeue(self, enqueued_delta, started_delta):
        job_model = self.env["queue.job"]
        stuck_jobs = job_model.search(
            self._get_stuck_jobs_domain(enqueued_delta, started_delta,)
        )
        return stuck_jobs

    def related_action_open_record(self):
        """Open a form view with the record(s) of the job.

        For instance, for a job on a ``product.product``, it will open a
        ``product.product`` form view with the product record(s) concerned by
        the job. If the job concerns more than one record, it opens them in a
        list.

        This is the default related action.

        """
        self.ensure_one()
        model_name = self.model_name
        records = self.env[model_name].browse(self.record_ids).exists()
        if not records:
            return None
        action = {
            "name": _("Related Record"),
            "type": "ir.actions.act_window",
            "view_mode": "form",
            "res_model": records._name,
        }
        if len(records) == 1:
            action["res_id"] = records.id
        else:
            action.update(
                {
                    "name": _("Related Records"),
                    "view_mode": "tree,form",
                    "domain": [("id", "in", records.ids)],
                }
            )
        return action

    def _test_job(self):
        _logger.info("Running test job.")


class RequeueJob(models.TransientModel):
    _name = "queue.requeue.job"
    _description = "Wizard to requeue a selection of jobs"

    def _default_job_ids(self):
        res = False
        context = self.env.context
        if context.get("active_model") == "queue.job" and context.get("active_ids"):
            res = context["active_ids"]
        return res

    job_ids = fields.Many2many(
        comodel_name="queue.job", string="Jobs", default=lambda r: r._default_job_ids()
    )

    def requeue(self):
        jobs = self.job_ids
        jobs.requeue()
        return {"type": "ir.actions.act_window_close"}


class SetJobsToDone(models.TransientModel):
    _inherit = "queue.requeue.job"
    _name = "queue.jobs.to.done"
    _description = "Set all selected jobs to done"

    def set_done(self):
        jobs = self.job_ids
        jobs.button_done()
        return {"type": "ir.actions.act_window_close"}


class JobChannel(models.Model):
    _name = "queue.job.channel"
    _description = "Job Channels"

    name = fields.Char()
    complete_name = fields.Char(
        compute="_compute_complete_name", store=True, readonly=True
    )
    parent_id = fields.Many2one(
        comodel_name="queue.job.channel", string="Parent Channel", ondelete="restrict"
    )
    job_function_ids = fields.One2many(
        comodel_name="queue.job.function",
        inverse_name="channel_id",
        string="Job Functions",
    )
    removal_interval = fields.Integer(
        default=lambda self: self.env["queue.job"]._removal_interval, required=True
    )

    _sql_constraints = [
        ("name_uniq", "unique(complete_name)", "Channel complete name must be unique")
    ]

    @api.depends("name", "parent_id.complete_name")
    def _compute_complete_name(self):
        for record in self:
            if not record.name:
                complete_name = ""  # new record
            elif record.parent_id:
                complete_name = ".".join([record.parent_id.complete_name, record.name])
            else:
                complete_name = record.name
            record.complete_name = complete_name

    @api.constrains("parent_id", "name")
    def parent_required(self):
        for record in self:
            if record.name != "root" and not record.parent_id:
                raise exceptions.ValidationError(_("Parent channel required."))

    def write(self, values):
        for channel in self:
            if (
                not self.env.context.get("install_mode")
                and channel.name == "root"
                and ("name" in values or "parent_id" in values)
            ):
                raise exceptions.UserError(_("Cannot change the root channel"))
        return super(JobChannel, self).write(values)

    def unlink(self):
        for channel in self:
            if channel.name == "root":
                raise exceptions.UserError(_("Cannot remove the root channel"))
        return super(JobChannel, self).unlink()

    def name_get(self):
        result = []
        for record in self:
            result.append((record.id, record.complete_name))
        return result


class JobFunction(models.Model):
    _name = "queue.job.function"
    _description = "Job Functions"
    _log_access = False

    JobConfig = namedtuple(
        "JobConfig",
        "channel "
        "retry_pattern "
        "related_action_enable "
        "related_action_func_name "
        "related_action_kwargs ",
    )

    def _default_channel(self):
        return self.env.ref("queue_job.channel_root")

    # TODO if 2 modules create an entry for the same method, do what:
    # * forbid? bad idea, prevent installing module
    # * hack create method to merge them, does it work regarding xmlids
    #   and uninstallation of modules?
    # * keep both records and let the user delete (or add "active" field)
    #   one of them, otherwise, take the first one

    name = fields.Char(index=True)
    channel_id = fields.Many2one(
        comodel_name="queue.job.channel",
        string="Channel",
        required=True,
        default=lambda r: r._default_channel(),
    )
    channel = fields.Char(related="channel_id.complete_name", store=True, readonly=True)
    retry_pattern = JobSerialized(string="Retry Pattern (serialized)", base_type=dict)
    edit_retry_pattern = fields.Text(
        string="Retry Pattern",
        compute="_compute_edit_retry_pattern",
        inverse="_inverse_edit_retry_pattern",
    )
    related_action = JobSerialized(string="Related Action (serialized)", base_type=dict)
    edit_related_action = fields.Text(
        string="Related Action",
        compute="_compute_edit_related_action",
        inverse="_inverse_edit_related_action",
    )

    @api.depends("retry_pattern")
    def _compute_edit_retry_pattern(self):
        for record in self:
            retry_pattern = record._parse_retry_pattern()
            record.edit_retry_pattern = str(retry_pattern)

    def _inverse_edit_retry_pattern(self):
        try:
            self.retry_pattern = ast.literal_eval(self.edit_retry_pattern or "{}")
        except (ValueError, TypeError):
            raise exceptions.UserError(self._retry_pattern_format_error_message())

    @api.depends("related_action")
    def _compute_edit_related_action(self):
        for record in self:
            record.edit_related_action = str(record.related_action)

    def _inverse_edit_related_action(self):
        try:
            self.related_action = ast.literal_eval(self.edit_related_action or "{}")
        except (ValueError, TypeError):
            raise exceptions.UserError(self._related_action_format_error_message())

    # TODO deprecated by :job-no-decorator:
    def _find_or_create_channel(self, channel_path):
        channel_model = self.env["queue.job.channel"]
        parts = channel_path.split(".")
        parts.reverse()
        channel_name = parts.pop()
        assert channel_name == "root", "A channel path starts with 'root'"
        # get the root channel
        channel = channel_model.search([("name", "=", channel_name)])
        while parts:
            channel_name = parts.pop()
            parent_channel = channel
            channel = channel_model.search(
                [("name", "=", channel_name), ("parent_id", "=", parent_channel.id)],
                limit=1,
            )
            if not channel:
                channel = channel_model.create(
                    {"name": channel_name, "parent_id": parent_channel.id}
                )
        return channel

    def job_default_config(self):
        return self.JobConfig(
            channel="root",
            retry_pattern={},
            related_action_enable=True,
            related_action_func_name=None,
            related_action_kwargs={},
        )

    def _parse_retry_pattern(self):
        try:
            # as json can't have integers as keys and the field is stored
            # as json, convert back to int
            retry_pattern = {
                int(try_count): postpone_seconds
                for try_count, postpone_seconds in self.retry_pattern.items()
            }
        except ValueError:
            _logger.error(
                "Invalid retry pattern for job function %s,"
                " keys could not be parsed as integers, fallback"
                " to the default retry pattern.",
                self.name,
            )
            retry_pattern = {}
        return retry_pattern

    @tools.ormcache("name")
    def job_config(self, name):
        config = self.search([("name", "=", name)], limit=1)
        if not config:
            return self.job_default_config()
        retry_pattern = config._parse_retry_pattern()
        return self.JobConfig(
            channel=config.channel,
            retry_pattern=retry_pattern,
            related_action_enable=config.related_action.get("enable", True),
            related_action_func_name=config.related_action.get("func_name"),
            related_action_kwargs=config.related_action.get("kwargs"),
        )

    def _retry_pattern_format_error_message(self):
        return _(
            "Unexpected format of Retry Pattern for {}.\n"
            "Example of valid format:\n"
            "{{1: 300, 5: 600, 10: 1200, 15: 3000}}"
        ).format(self.name)

    @api.constrains("retry_pattern")
    def _check_retry_pattern(self):
        for record in self:
            retry_pattern = record.retry_pattern
            if not retry_pattern:
                continue

            all_values = list(retry_pattern) + list(retry_pattern.values())
            for value in all_values:
                try:
                    int(value)
                except ValueError:
                    raise exceptions.UserError(
                        record._retry_pattern_format_error_message()
                    )

    def _related_action_format_error_message(self):
        return _(
            "Unexpected format of Related Action for {}.\n"
            "Example of valid format:\n"
            '{{"enable": True, "func_name": "related_action_foo",'
            ' "kwargs" {{"limit": 10}}}}'
        ).format(self.name)

    @api.constrains("related_action")
    def _check_related_action(self):
        valid_keys = ("enable", "func_name", "kwargs")
        for record in self:
            related_action = record.related_action
            if not related_action:
                continue

            if any(key not in valid_keys for key in related_action):
                raise exceptions.UserError(
                    record._related_action_format_error_message()
                )

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        self.clear_caches()
        return records

    def write(self, values):
        res = super().write(values)
        self.clear_caches()
        return res

    def unlink(self):
        res = super().unlink()
        self.clear_caches()
        return res

    # TODO deprecated by :job-no-decorator:
    def _register_job(self, model, job_method):
        func_name = job_function_name(model, job_method)
        if not self.search_count([("name", "=", func_name)]):
            channel = self._find_or_create_channel(job_method.default_channel)
            self.create({"name": func_name, "channel_id": channel.id})

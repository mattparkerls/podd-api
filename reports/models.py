# -*- encoding: utf-8 -*-
import calendar
import copy
import json
import re
import operator
import threading
import uuid
import datetime

from asteval import Interpreter
from django.contrib.gis.db.models import GeoManager
from django.forms import model_to_dict
from django_redis import get_redis_connection
from cacheops import invalidate_obj
import itertools
from treebeard.exceptions import NodeAlreadySaved

from celery.contrib.methods import task_method
from crum import get_current_user
from django.core import validators
from django.core.urlresolvers import reverse
from django.db import transaction
from django.db.models import Q, QuerySet, Prefetch, Count
from django.db.models.fields import related
from django.template import Template, Context
from django.template.defaultfilters import striptags
from django.template.loader import render_to_string
from django.utils import timezone

import requests
from treebeard.exceptions import NodeAlreadySaved
from treebeard.ns_tree import NS_Node, NS_NodeManager, get_result_class

from django.conf import settings
from django.contrib.gis.db import models
from django.utils.translation import ugettext_lazy as _

from taggit.managers import TaggableManager

from accounts.models import User, Authority, user_can_edit_basic_check, Configuration
from accounts.serializers import UserSerializer
from common.constants import PRIORITY_CHOICES, NEWS_TYPE_NEWS, NEWS_TYPE_SUBSCRIBE_AUTHORITY, USER_STATUS_VOLUNTEER, \
    USER_STATUS_ADDITION_VOLUNTEER, \
    PRIORITY_IGNORE, PRIORITY_OK, PRIORITY_CONTACT, PRIORITY_FOLLOW, PRIORITY_CASE, STATUS_CHOICES, STATUS_PUBLISH, \
    STATUS_DELETE, INVESTIGATION_TYPE
from common.decorators import domain_celery_task
from common.functions import safe_eval, get_system_user, randstr, filter_permitted_administration_areas_and_descendants, \
    get_public_area, get_administration_area_and_descendants, clean_phone_numbers, make_hash
from common.models import AbstractCachedModel, DomainMixin, DomainManager, get_current_domain_id, \
    Domain
from feed.functions import get_public_feed_key
from logs.models import LogItem
from mentions.models import Mention
from mentions.serializers import MentionSerializer
from notifications.models import FollowUp, NotificationTemplate
from plans.models import Plan, PlanReport
from podd.celery import app, DomainTask
from reports.pub_tasks import publish_mention, publish_comment, publish_report, publish_report_state


class ReportTypeCategory(DomainMixin):
    name = models.CharField(max_length=512)
    code = models.CharField(max_length=255)
    description = models.TextField(null=False, blank=True)

    #image_url = models.URLField()
    #thumbnail_url = models.URLField()


    class Meta:
        unique_together = ("domain", "code")

    def __unicode__(self):
        return self.name


class ReportType(AbstractCachedModel, DomainMixin):
    name = models.CharField(max_length=200)
    code = models.CharField(max_length=100, unique=True)
    form_definition = models.TextField(null=False, blank=True)
    version = models.IntegerField(default=1)
    template = models.TextField(blank=True, default='')
    weight = models.FloatField(blank=True, null=True)
    followable = models.NullBooleanField(default=True)
    follow_days = models.IntegerField(blank=True, null=True)
    django_template = models.TextField(blank=True, default='', null=True)
    summary_template = models.TextField(blank=True, default='', null=True)

    authority = models.ForeignKey(Authority, related_name='report_type_authority', blank=True, null=True)
    default_state = models.ForeignKey('reports.ReportState', related_name='report_type_default_state', blank=True, null=True)

    category = models.ForeignKey(ReportTypeCategory, related_name='report_type_category', blank=True, null=True)

    report_pre_save = models.TextField(null=True, blank=True)
    is_system = models.BooleanField(default=False)

    cached_vars = [('authority', True), 'form_definition', 'summary_template', 'version']

    graph_node = True
    graph_relations = ['authority']


    class Meta:
        ordering = ['weight', 'name']

    def __unicode__(self):
        return self.name

    def get_schema_fields(self):

        if not self.form_definition:
            return {}

        definition = json.loads(self.form_definition)
        questions = definition.get('questions') or []

        # merge duplicated fields
        fields = {}
        for question in questions:

            field_type = 'int' if question['type'] == 'integer' else 'String'

            fields[question['name']] = field_type
            if question.get('hiddenName'):
                fields[question['hiddenName']] = field_type

        # force id field to int type
        fields['id'] = 'int'
        fields['parent'] = 'int'

        # extra fields
        fields['areaId'] = 'int'
        fields['authorityId'] = 'int'
        fields['createdById'] = 'int'
        fields['createdAt'] = 'long'
        fields['date'] = 'long'
        fields['timestamp'] = 'long'
        fields['incidentDate'] = 'long'
        fields['createdByDateJoined'] = 'long'
        fields['latitude'] = 'double'
        fields['longitude'] = 'double'

        return fields

    def get_schema_name(self):
        return self.code.replace('-', '_').replace('.', '_')

    def create_cep(self):

        payload = {
            'name': 'reportType_%s_report' % self.get_schema_name(),
            'fields': self.get_schema_fields(),
            'dropIfExists': False
        }

        if not settings.ESPER_CONNECTION_URL:
            return

        resp_schema = requests.post('%sschema' % settings.ESPER_CONNECTION_URL, data=json.dumps(payload))

        print 'Schema state: report ================='
        print payload
        print resp_schema
        print '========================'

    def save(self, set_default_state=False, *args, **kwargs):

        if not self.code:
            self.code = randstr()

        if not set_default_state:
            self.create_cep()

        is_new = not self.id

        hash_new = make_hash(json.loads(self.form_definition))

        if is_new:
            hash_old = ''
        else:
            try:
                hash_old = make_hash(json.loads(self.var_cache['form_definition']))
            except ValueError:
                hash_old = ''

        old_version = self.var_cache.get('version')
        to_increase_version_number = False
        if old_version:
            if hash_new != hash_old and self.version == old_version:
                # try not to override version number if provided. ^
                to_increase_version_number = True
        else:
            if hash_new != hash_old:
                to_increase_version_number = True

        if to_increase_version_number:
            self.version += 1

        if self.weight is None:
            try:
                self.weight = ReportType.objects.filter(weight__isnull=False).latest('weight').weight + 1
            except ReportType.DoesNotExist:
                self.weight = 0

        super(ReportType, self).save(*args, **kwargs)

        if not set_default_state:
            if is_new:
                self.default_state = ReportState.objects.create(report_type=self, name='Report', code='report')
                self.save(set_default_state=True)

            #if self.authority:
                #self.authority.report_types.add(self) # response first  before long task executed
                #self.authority.update_stores.delay(field_names=['report_types'])

            #if self.var_cache['authority'] and self.var_cache['authority'] != self.authority.id:
                #old_authority = Authority.objects.get(id=self.var_cache['authority'])
                #old_authority.update_stores.delay(field_names=['report_types'])

        # if self.summary_template:
        #     # Update header template
        #     headers = {}
        #
        #     # Remove this report type
        #     try:
        #         header_template = Configuration.objects.get(system='web.template.report', key='summary_month').value
        #         headers = json.loads(header_template)
        #         for key, value in headers.iteritems():
        #             if key == 'ordering':
        #                 continue
        #             try:
        #                 value[self.id] = -1
        #             except (KeyError, ValueError):
        #                 pass
        #
        #     except Configuration.DoesNotExist:
        #         pass
        #
        #     # Set new order
        #     template = json.loads(self.summary_template)
        #     for key, value in template.iteritems():
        #         weight = 1
        #         if 'weight' in value:
        #             weight = value['weight']
        #         try:
        #             headers[key][self.id] = weight
        #         except (KeyError, ValueError):
        #             headers[key] = {self.id: weight}
        #
        #     # Update ordering header list
        #     ordering = {}
        #     for key, value in headers.iteritems():
        #         ordering_fields = sorted(value.items(), key=operator.itemgetter(1), reverse=True)
        #         if ordering_fields:
        #             ordering[key] = ordering_fields[0][1]
        #
        #     headers['ordering'] = ordering
        #
        #     # Save to config
        #     header_template, create = Configuration.objects.get_or_create(system='web.template.report', key='summary_month')
        #     header_template.value = json.dumps(headers)
        #     header_template.save()

    def user_can_edit(self, user):
        return user_can_edit_basic_check(user, self.authority and self.authority.users.filter(id=user.id).count() > 0)


class ReportState(DomainMixin):
    report_type = models.ForeignKey(ReportType, related_name='report_state_report_type')
    name = models.CharField(max_length=200)
    code = models.CharField(max_length=100)
    description = models.TextField(null=True, blank=True) # Explain another user for reusable state
    weight = models.PositiveIntegerField(default=0)

    allow_to_states = models.ManyToManyField('self', null=True, blank=True) # TODO: implement on report state change

    class Meta:
        ordering = ['weight', 'id']

    def __unicode__(self):
        return '%s %s' % (self.report_type.name, self.name)

    def get_schema_name(self):
        return 'reportType_%s_%s' % (self.report_type.get_schema_name(), self.code)

    def create_cep(self):
        #self.full_clean()  # validate schema name(code) before push to Esper

        payload = {
            'name': self.get_schema_name(),
            'fields': self.report_type.get_schema_fields(),
            'dropIfExists': False
        }
        if not settings.ESPER_CONNECTION_URL:
            return
        resp = requests.post('%sschema' % settings.ESPER_CONNECTION_URL, data=json.dumps(payload))

        #print 'Schema state: %s =================' % self.get_schema_name()
        #print payload
        #print resp
        #print '========================'

    @property
    def from_states(self):
        states = set()
        for case_def in CaseDefinition.objects.filter(report_type=self.report_type, to_state=self):
            states.add(case_def.from_state)

        return states

    @property
    def to_states(self):
        states = set()
        for case_def in CaseDefinition.objects.filter(report_type=self.report_type, from_state=self):
            states.add(case_def.to_state)

        return states

    def save(self, *args, **kwargs):
        super(ReportState, self).save(*args, **kwargs)
        self.create_cep()


    def user_can_edit(self, user):
        return user_can_edit_basic_check(user, self.report_type and self.report_type.authority and self.report_type.authority.admins.filter(id=user.id).count() > 0)


class CaseDefinition(DomainMixin):
    report_type = models.ForeignKey(ReportType, related_name='case_definition_report_type')
    from_state = models.ForeignKey(ReportState, related_name='from_state', null=True, blank=True, help_text='If none, default state is report')
    to_state = models.ForeignKey(ReportState, related_name='to_state')
    epl = models.TextField(verbose_name=_('EPL where'), help_text='sickCount > 10, win.areaId = currentEvent.areaId group by win.areaId having (sum(win.sickCount) + sum(win.deadCount)) > 3 (extra params are : win, currentEvent)') # Esper query processing language
    code = models.CharField(
        max_length=255,
        unique=True,
        validators=[
            validators.RegexValidator(re.compile('(?:^[A-Za-z][A-Za-z0-9]*)+'), _('Enter a valid code.'), 'invalid')
        ]
    ) # For generate schema name on Esper
    description = models.TextField(null=True, blank=True) # Human language
    drop_if_exists = models.BooleanField(verbose_name=_('Rebuild Esper Schema ?'), default=False) # Don't use now

    accumulate = models.BooleanField(default=False)
    window = models.TextField(verbose_name=_('EPL window criteria'), help_text='win:ext_timed(date, 7 day)', blank=True, null=True) # Esper query processing language
    auto_create_report = models.BooleanField(default=False)

    _extra_info = None

    class Meta:
        ordering = ['report_type', 'description']

    def __unicode__(self):
        return '%s [%s > %s] %s' % (self.description, self.from_state.name, self.to_state.name, self.code)

    def get_from_state_code(self):
        return (self.from_state and self.from_state.code) or 'report'

    def get_to_state_code(self):
        return (self.to_state and self.to_state.code) or 'report'

    def get_schema_from_name(self):
        return 'reportType_%s_%s' % (self.report_type.get_schema_name(), self.get_from_state_code())

    def get_schema_to_name(self):
        return 'reportType_%s_%s' % (self.report_type.get_schema_name(), self.get_to_state_code())

    def complete_epl(self):
        epl = self.epl

        for code in re.findall(r'@\[case:([A-Za-z0-9]*)\]', epl):
            epl = epl.replace('@[case:%s]' % code, '(%s)' % CaseDefinition.objects.get(code=code, accumulate=False).complete_epl())

        return epl

    def create_cep(self):

        #self.full_clean()  # validate schema name(code) before push to Esper





        if self.accumulate and self.window:

            if self.auto_create_report:
                stmt = "select win.id as id, '%s' as createReportUrl from %s.%s as win, %s.std:lastevent() as currentEvent where %s"

                response_url_pattern = '%s%s' % (
                    settings.ESPER_RESPONSE_BASE_URL,
                    'report/protect-create-with-state/%s/%s/%s/' % (
                        settings.UPDATE_REPORT_STATE_KEY,
                        self.get_to_state_code(),
                        self.code
                    )
                )

            else:
                stmt = "select promoteState(win.id, '%s') as id from %s.%s as win, %s.std:lastevent() as currentEvent where %s"

                response_url_pattern = '%s%s' % (
                    settings.ESPER_RESPONSE_BASE_URL,
                    'report/{{id}}/protect-update-state/%s/%s/%s/' % (
                        settings.UPDATE_REPORT_STATE_KEY,
                        self.get_to_state_code(),
                        self.code
                    )
                )


            stmt = stmt % (
                response_url_pattern,
                self.get_schema_from_name(),
                self.window,
                self.get_schema_from_name(),
                self.complete_epl()
            )


        else:

            response_url_pattern = '%s%s' % (
                settings.ESPER_RESPONSE_BASE_URL,
                'report/{{id}}/protect-update-state/%s/%s/%s/' % (
                    settings.UPDATE_REPORT_STATE_KEY,
                    self.get_to_state_code(),
                    self.code
                )
            )

            stmt = "select promoteState(id, '%s') as id from %s.std:lastevent() where %s" % (
                response_url_pattern,
                self.get_schema_from_name(),
                self.complete_epl()
            )

        payload = {
            'stmt': stmt
        }

        if not settings.ESPER_CONNECTION_URL:
            return

        resp = requests.post('%squery' % settings.ESPER_CONNECTION_URL, data=json.dumps(payload))

        print 'Query =================='
        print payload
        print resp
        print '========================'

        # Auto regenerate schema from report, state when Esper restart
        if resp.status_code == 500:
            if "Failed to resolve event type: Event type or class named '%s' was not found" % self.get_schema_from_name() in resp.text:
                self.report_type.create_cep()
                for state in self.report_type.report_state_report_type.all():
                    state.create_cep()
                self.create_cep()

        # Force to not drop when edit in the next time
        self.drop_if_exists = False

    def save(self, *args, **kwargs):
        super(CaseDefinition, self).save(*args, **kwargs)
        self.create_cep()


    def user_can_edit(self, user):
        return user_can_edit_basic_check(user, self.report_type and self.report_type.authority and self.report_type.authority.users.filter(id=user.id).count() > 0)



class MixAdministrationAreaManager(DomainManager, NS_NodeManager):
    pass

class CustomNS_Node(NS_Node):

    parent = models.ForeignKey('self', null=True, blank=True)

    class Meta:
        abstract = True

    def get_parent(self, update=False):
        """
        :returns: the parent node of the current node object.
            Caches the result in the object itself to help in loops.
        """

        if self.parent:
            return self.parent

        if self.is_root():
            return
        try:
            if update:
                del self._cached_parent_obj
            else:
                self.parent = self._cached_parent_obj
                self.save()
                return self._cached_parent_obj
        except AttributeError:
            pass
        # parent = our most direct ancestor
        self._cached_parent_obj = self.get_ancestors().reverse()[0]
        self.parent = self._cached_parent_obj
        self.save()

        return self._cached_parent_obj


# NS_Node deprecate
class AdministrationArea(CustomNS_Node, AbstractCachedModel, DomainMixin):
    name = models.CharField(max_length=200)
    location = models.PointField()
    area_code = models.CharField(max_length=10, null=True, blank=True)
    # multi-polygon field, named as referred from @koyoyo's dumped file.
    mpoly = models.MultiPolygonField(srid=4326, null=True, blank=True)
    address = models.TextField(default='', blank=True)
    code = models.CharField(max_length=100, blank=True)
    weight = models.FloatField(blank=True, null=True, default=0)
    authority = models.ForeignKey('accounts.Authority', related_name='area_authority', null=True, blank=True)

    contacts = models.TextField(null=True, blank=True)
    remark = models.TextField(null=True, blank=True)

    node_order_by = ['name']
    parent_name = None
    cached_vars = [('authority', True)]

    qgis_id = models.CharField(max_length=200, null=True, blank=True)
    objects = MixAdministrationAreaManager()

    graph_node = True
    graph_fields = ['address', 'location.json', 'code', 'weight']
    graph_relations = ['authority']

    def __unicode__(self):
        return self.name

    def warm_cache_public_feed(self):
        reports = Report.objects.filter(administration_area=self)
        for report in reports:
            report.add_to_public_feed()

        # Warm curated_reports
        reports = self.curated_reports.all()
        for report in reports:
            report.curate_in_administration_area(self)

    def get_contacts(self, keys=[]):

        contacts = self.contacts or ''

        if len(keys) > 0:
            try:
                contacts = json.loads(contacts)

                for key in keys:
                    contacts = contacts.get(key) or ''
                    if not contacts:
                        break

            except ValueError:
                contacts = ''

        return clean_phone_numbers(contacts)


    def save(self, *args, **kwargs):
        if not self.id and (not self.lft or not self.rgt):
            # Need to force to assign domain value here, because NS_Tree will raise
            # ValidationError before DomainMixin assign it.
            self.domain = Domain.objects.get(id=get_current_domain_id())
            obj_dict = {}
            for field in self._meta.fields:
                try:
                    obj_dict[field.name] = getattr(self, field.name)
                except Domain.DoesNotExist:
                    pass

            obj = self.add_root(**obj_dict)
            #if self.authority:
            #    self.authority.administration_areas.add(obj)

            self.id = obj.id
            self.tree_id = obj.tree_id
            self.lft = obj.lft
            self.rgt = obj.rgt
        else:
            super(AdministrationArea, self).save(*args, **kwargs)

        #if self.authority:
        #    self.authority.update_stores.delay(field_names=['administration_areas'])

        #if self.var_cache['authority'] and self.var_cache['authority'] != self.authority.id:
        #    old_authority = Authority.objects.get(id=self.var_cache['authority'])
        #    old_authority.update_stores(field_names=['administration_areas'])

    def user_can_edit(self, user):
        return user_can_edit_basic_check(user, self.authority and self.authority.admins.filter(id=user.id).count() > 0)


class GeoDomainManager(GeoManager, DomainManager):
    pass

class Report(AbstractCachedModel, DomainMixin):
    report_id = models.IntegerField()                               # id on client side
    guid = models.TextField(unique=True)                            # unique identifier
    report_location = models.PointField(null=True, blank=True)      # location when reporting
    administration_location = models.PointField(null=True, blank=True)                   # location of adminstration area
    administration_area = models.ForeignKey('AdministrationArea', related_name='reports', null=True, blank=True)
    type = models.ForeignKey('ReportType', related_name='reports')
    parent = models.ForeignKey('self', null=True, blank=True, related_name='children')
    date = models.DateTimeField()
    incident_date = models.DateField()
    form_data = models.TextField(blank=True)
    original_form_data = models.TextField(blank=True)
    remark = models.TextField(blank=True, default='')
    negative = models.BooleanField(default=False)
    test_flag = models.NullBooleanField()

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='report_created_by')
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='report_updated_by', null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    state = models.ForeignKey('ReportState', null=True, blank=True, related_name='report_state')

    cached_vars = ['state', 'form_data', 'test_flag']

    priority = models.PositiveIntegerField(default=0, choices=PRIORITY_CHOICES)
    rendered_form_data = models.TextField(blank=True, default='')
    rendered_original_form_data = models.TextField(blank=True, default='')

    is_public = models.BooleanField(default=False)
    is_anonymous = models.BooleanField(default=False)
    curated_in = models.ManyToManyField('AdministrationArea', related_name="curated_reports", null=True, blank=True)

    comment_count = models.PositiveIntegerField(default=0)
    like_count = models.PositiveIntegerField(default=0)
    me_too_count = models.PositiveIntegerField(default=0)

    _plan_reports = []

    _state_code = None
    _plan_accepted_list = None

    _test_flag = False
    _state_changed_by_case = False

    tags = TaggableManager()

    objects = GeoDomainManager()

    class Meta:
        ordering = ['-updated_at', ]

    def __unicode__(self):
        if self.test_flag:
            report = u'ทดสอบรายงาน'
        else:
            if self.negative:
                report = u'รายงานผิดปกติ'
            else:
                report = u'รายงานไม่พบเหตุผิดปกติ'

        return u'%s #%s' % (report, self.report_id)

    def delete(self, *args, **kwargs):
        tmp_id = self.id
        tmp_curated_in = [area.id for area in self.curated_in.all()]
        super(Report, self).delete(*args, **kwargs)
        self.id = tmp_id
        self.pk = tmp_id
        self.curated_in = AdministrationArea.objects.filter(id__in=tmp_curated_in)
        self.remove_from_public_feed()

    def get_latest_flag(self):
        from flags.models import Flag
        flags = Flag.objects.filter(comment__report=self)
        if flags.exists():
            return flags.latest('id')
        return None

    def get_state_code(self):

        if not self._state_code: # cache for async
            self._state_code = (self.state and self.state.code) or 'report'

        return self._state_code

    @property
    def is_curated(self):
        return self.curated_in.all().count() > 0

    @property
    def state_code(self):
        return self.get_state_code()

    @property
    def state_name(self):
        return (self.state and self.state.name) or 'Report'

    def get_type_code(self):
        return self.type.code

    @property
    def type_code(self):
        return self.get_type_code()

    def get_schema_name(self):
        if self.parent:
            state_code = self.parent.get_state_code()
        else:
            state_code = self.get_state_code()

        return 'reportType_%s_%s' % (self.type.get_schema_name(), state_code)

    @property
    def get_tags(self):
        return self.tags.values_list('name', flat=True)

    @property
    def data(self):
        return type('ReportData', (object,), json.loads(self.form_data))

    @property
    def boundary(self):

        if self.report_location:

            l = self.report_location
            radius = 0.01
            return {
                'bottom': l.x + radius,
                'left': l.y - radius,
                'top': l.x - radius,
                'right': l.y + radius
            }

    def adjust_incident_date(self):
        try:
            threshold = settings.report.threshold
        except AttributeError:
            threshold = datetime.timedelta(days=30)

        now = timezone.now()
        past_date_limit = now - threshold
        # need to convert to timezone aware
        if timezone.is_naive(self.date):
            self.date = timezone.make_aware(self.date, timezone.utc)

        if (
            self.incident_date < past_date_limit.date() or
            self.date < past_date_limit
           ):
            # keep original date in form_data
            form_data = json.loads(self.form_data)
            form_data['_invalid_report_incident_date'] = str(self.incident_date)
            form_data['_invalid_report_date'] = str(self.date)
            self.form_data = json.dumps(form_data)
            # then adjust date
            self.incident_date = now.date()
            self.date = now

    def create_cep(self):

        # See https://bitbucket.org/opendream/podd-cep

        data = json.loads(self.form_data)
        data['id'] = self.id

        if self.parent:
            data['parent'] = self.parent.id
        else:
            data['parent'] = self.id

        schema_fields = self.type.get_schema_fields()

        # Prepare for make sure data will not error before publish event to Esper
        for field_name, value in data.copy().iteritems():
            try:
                field_type = schema_fields[field_name]
                value = data[field_name]
                if field_type == 'int':
                    data[field_name] = int(value)

            except KeyError:
                del (data[field_name])


        data['areaId'] = self.administration_area.id
        data['authorityId'] = self.administration_area.authority.id
        data['createdById'] = self.created_by.id
        data['createdAt'] = calendar.timegm(self.created_at.timetuple()) * 1000
        data['date'] = calendar.timegm(self.date.timetuple()) * 1000
        data['incidentDate'] = calendar.timegm(self.incident_date.timetuple()) * 1000
        data['createdByDateJoined'] = calendar.timegm(self.created_by.date_joined.timetuple()) * 1000

        if self.report_location:
            data['latitude'] = self.report_location.y
            data['longitude'] = self.report_location.x


        payload = {
            'name': self.get_schema_name(),
            'converter': schema_fields,
            'data': data
        }

        if not settings.TESTING:
            from reports.tasks import report_cep

            # Use thread or async because db transaction hang jaa
            #from podd.celery import app
            #report_cep.delay(report_id=self.id, payload=payload, countdown=5)
            report_cep.apply_async((self.id, payload), countdown=5)

    def prepare_condition(self, condition):
        condition = re.sub(r'(([\'\"]).*?\2)', r'u\1', condition)
        return condition

    def _check_report_condition(self, condition, plan=None, authority=None, template=None):

        plan = plan or type('Plan', (object,), {'code': None, 'name': None})
        #authority = authority or type('Authority', (object,), {'id': 0, 'code': None, 'name': None})

        symtable = {
            'IGNORE': str(PRIORITY_IGNORE),
            'OK': str(PRIORITY_OK),
            'CONTACT': str(PRIORITY_CONTACT),
            'FOLLOW': str(PRIORITY_FOLLOW),
            'CASE': str(PRIORITY_CASE),
            'report': self,
            'plan': plan,
            'authority': authority
        }

        prepared_condition = self.prepare_condition(condition)
        result = safe_eval(prepared_condition, symtable)

        if result and plan.code and template:
            template.accepted_plan = plan

        return result

    def check_report_condition(self, condition, plan_list=[], plan_valid_list=[], authority=None, template=None):
        result = self._check_report_condition(condition, authority=authority)
        for plan in plan_list:

            valid_plan = self._check_report_condition(condition, plan, authority, template=template)
            if valid_plan and plan not in plan_valid_list:
                plan_valid_list.append(plan)

            result = result or valid_plan

        return result

    def accepted_plans(self):

        if self._plan_accepted_list is None:
            # find plan accepted condition
            plan_accepted_list = []
            for plan in Plan.objects.all():
                condition = plan.condition
                if self.check_report_condition(condition):
                    plan_accepted_list.append(plan)

            self._plan_reports = []
            for plan in plan_accepted_list:
                plan_report = PlanReport.objects.create(plan=plan, report=self)
                plan._current_log = json.loads(plan_report.log)
                self._plan_reports.append(plan_report)

            self._plan_accepted_list = plan_accepted_list

        return self._plan_accepted_list


    def accepted_authority_plan_level(self, authority, plan_code='', level_code=''):

        if not authority:
            return True

        for plan_report in self._plan_reports:
            if plan_code == plan_report.plan.code:

                log = json.loads(plan_report.log)
                level_authority_ids = log['level_authority_ids'].get(level_code) or []
                if authority.id in level_authority_ids:
                    return True

        return False


    def create_comment_plans(self):

        from reports.models import ReportComment
        from common.functions import get_system_user

        try:
            template_for_notification = Configuration.objects.get(system='web.template.report',
                                                                  key='comment_plans').value
        except Configuration.DoesNotExist:
            template_for_notification = u'เริ่มแผน "%(name)s" [plan-report:%(id)d]'

        system_user = get_system_user()

        for plan_report in self._plan_reports:

            # Send to comment

            message = template_for_notification % {
                'name': plan_report.plan.name,
                'id': plan_report.id
            }

            comment = ReportComment.objects.create(
                report=self,
                message=message,
                created_by=system_user,
            )



    def create_report_notification(self, types=None, plan_accepted_list=[]):

        from notifications.models import NotificationTemplate

        # find owner area
        authority = self.administration_area.authority
        if not authority:
            return

        types = types or [NotificationTemplate.TYPE_REPORT, NotificationTemplate.TYPE_PRIVATE]

        # find notification templates accepted condition
        notification_template_list = NotificationTemplate.objects.filter(type__in=types)

        notification_template_accepted_list = []
        for notification_template in notification_template_list:
            # We don't check condition for any delayed notify. The condition will be check later.
            if notification_template.type == NotificationTemplate.TYPE_DELAYED_FOLLOW_UP:
                now = timezone.now()
                delayed_hours = notification_template.delayed_time
                follow_up_data = {
                    'report': self,
                    'template': notification_template,
                    'authority': authority,
                    'date': now + datetime.timedelta(hours=delayed_hours)
                }
                FollowUp.objects.create(**follow_up_data)
                continue

            condition = notification_template.condition
            plan_and_notification_template_accepted_list = []
            if self.check_report_condition(condition, plan_accepted_list, plan_and_notification_template_accepted_list, template=notification_template):
                # for find plan areas when send notifications
                notification_template.plan_valid_list = plan_and_notification_template_accepted_list
                notification_template_accepted_list.append(notification_template)

        self.create_notification(notification_template_accepted_list, authority)

    def create_notification(self, notification_template_accepted_list, authority=None):

        authority = authority or self.administration_area.authority


        # for graph db
        subscriber_ids = set(authority.get_subscribers_all())

        for plan_report in self._plan_reports:
            # merge all authority for auto subscribe on the fly when plan accepted
            subscriber_ids |= set(itertools.chain(*json.loads(plan_report.log)['level_authority_ids'].values()))

        # find subscribers
        subscriber_list = Authority.objects.filter(id__in=subscriber_ids).exclude(area_authority=self.administration_area)

        sents = {}
        for accepted in notification_template_accepted_list:
            sents[accepted.get_comment_render()] = []

        stamps = []

        # check and send notification to owner authority
        self._create_notification(notification_template_accepted_list, [authority], sents, stamps=stamps, inherits_send=True)
        # check and send notification to authority subscribers
        notification_template_accepted_subscribe_list = [t for t in notification_template_accepted_list if t.type != NotificationTemplate.TYPE_PRIVATE]


        self._create_notification(notification_template_accepted_subscribe_list, subscriber_list, sents, stamps=stamps, subscribe_authority=authority)

        self.create_comment_notification(sents)

    def create_comment_notification(self, sents):

        # Send to comment
        from reports.models import ReportComment

        for template, receive_users in sents.iteritems():
            # print "send notification to ------ ", self.to
            try:
                template_for_notification =  Configuration.objects.get(system='web.template.report', key='comment_notification').value
            except Configuration.DoesNotExist:
                template_for_notification = u'@[%(username)s] ได้ส่งข้อความแจ้งเตือน "%(template)s" ไปยัง %(receive_users)s'

            from common.functions import get_system_user
            system_user = get_system_user()

            message = template_for_notification % {
                'username': system_user.username,
                'template': template,
                'receive_users': ', '.join(receive_users)
            }

            comment = ReportComment.objects.create(
                report = self,
                message = message,
                created_by = system_user,
            )

    def _create_notification(self, notification_template_accepted_list, authority_list, sents, subscribe_authority=None, stamps=[], inherits_send=False, direct_to_list=None):


        from notifications.models import NotificationAuthority, Notification

        notification_type = NEWS_TYPE_NEWS
        if subscribe_authority:
            notification_type = NEWS_TYPE_SUBSCRIBE_AUTHORITY


        for accepted in notification_template_accepted_list:
            for authority in authority_list:

                stamps.append(authority.id) # Check circularaccepted_authority_plan_level
                allow = True
                # Check template enabled for authority
                try:
                    notification_authority = NotificationAuthority.objects.get(template__id=accepted.id, authority__id=authority.id)
                    if not notification_authority.to:
                        allow = False
                except NotificationAuthority.DoesNotExist:
                    allow = False

                # check condition with authority
                if direct_to_list is None and not self.check_report_condition(accepted.condition, plan_list=self.accepted_plans(), authority=authority):
                    allow = False





                if allow:
                    if direct_to_list is not None:
                        to_list = direct_to_list
                    else:
                        to_list = notification_authority.to.split(',')

                    for to in to_list:

                        to = to.strip()


                        users = list(User.objects.filter(Q(email=to) | Q(username=to)).order_by('-last_login')[0:1])

                        notification_data = {
                            'report': self,
                            'notification_authority': notification_authority,
                            'to': to, # TODO: change to receive_user and fix unittest
                            'type': notification_type,
                            'subscribe_authority': subscribe_authority
                        }

                        receive_user = False



                        # Exist users
                        if len(users):
                            for user in users:
                                notification_data['receive_user'] = user
                                receive_user = '@[%s]' % user.username

                        # Detect phone number for send sms
                        elif re.match(r'^[0-9-]*$', to):
                            clean_phone_number = re.sub('[^0-9]+', '', to)
                            notification_data['to'] = clean_phone_number
                            notification_data['anonymous_send'] = Notification.SMS_ONLY

                            receive_user = '@[tel:%s]' % to

                        elif re.match(r'[^@]+@[^@]+\.[^@]+', to):
                            notification_data['anonymous_send'] = Notification.EMAIL_ONLY

                            receive_user = '@[email:%s]' % to

                        elif to == '@[contacts]':
                            to_list.extend(self.administration_area.get_contacts())

                        elif re.match(r'^@\[contacts:(.*)\]', to):
                            keys = re.match(r'^@\[contacts:(.*)\]', to).groups()[0].split(':')
                            to_list.extend(self.administration_area.get_contacts(keys=keys))

                        elif re.match(r'^@\[plan:(.*)\]', to):
                            keys = re.match(r'^@\[plan:(.*)\]', to).groups()[0].split(':')
                            level = keys[0]
                            keys = keys[1:]

                            # log template to plan report for each levels
                            for plan_report in self._plan_reports:
                                if not plan_report.level_templates.get(level):
                                    plan_report.level_templates[level] = []
                                plan_report.level_templates[level].append(accepted)

                            for plan in accepted.plan_valid_list: # The config plans should be single plan

                                level_areas = plan.level_areas(self.administration_area)

                                for area in level_areas.get(level) or []:
                                    to_list.extend(area.get_contacts(keys=keys))

                        elif re.match(r'^@\[template:(.*)\]', to):
                            template_id = re.match(r'^@\[template:(.*)\]', to).groups()[0]

                            try:
                                enabled = NotificationAuthority.objects.get(template__id=template_id, authority__id=authority.id)
                                if enabled.to:
                                    to_list.extend(enabled.to.split(','))

                            except NotificationAuthority.DoesNotExist:
                                pass


                        if receive_user and not receive_user in sents[accepted.get_comment_render()]:
                            sents[accepted.get_comment_render()].append(receive_user)
                            Notification.objects.create(**notification_data)
                            Notification.plan = accepted.accepted_plan


                for inherit in authority.inherits.exclude(id__in=stamps):
                    self._create_notification(notification_template_accepted_list, [inherit], sents, inherits_send=True, stamps=stamps, direct_to_list=direct_to_list)

    def create_reporter_notification(self, types=None):

        from notifications.models import NotificationAuthority, Notification, NotificationTemplate

        types = types or [NotificationTemplate.TYPE_REPORTER_FEEDBACK, NotificationTemplate.TYPE_NOTIFY_FOLLOW_UP]
        created_on = self.created_at

        # notification_authority_list = NotificationAuthority.objects.filter(
        #     template__type__in=types
        # ).extra(
        #     where=['notifications_notificationtemplate.authority_id = notifications_notificationauthority.authority_id']
        # )

        authority = self.administration_area.authority

        template_list = NotificationTemplate.objects.filter(type__in=types).extra(where=['''id in (
            SELECT na.template_id
            FROM notifications_notificationauthority na, reports_administrationarea a
            WHERE na.authority_id = a.authority_id AND a.id = %d
        )''' % authority.id])

        # Check enable notifications
        for template in template_list:

            # Prevent send feedback to inherits authorities when children authorities create reporter feedback
            # if authority.administration_areas.filter(id=self.administration_area.id).count() == 0:
            #     continue

            if (template.type != NotificationTemplate.TYPE_DELAYED_FOLLOW_UP and
                    not self.check_report_condition(template.condition)):
                continue

            notification_authority = NotificationAuthority.objects.filter(template=template, authority=authority).latest('id')

            # =======================================
            # Feedback
            # =======================================
            if template.type == NotificationTemplate.TYPE_REPORTER_FEEDBACK:
                notification_data = {
                    'report': self,
                    'notification_authority': notification_authority,
                    'receive_user': self.created_by,
                    'to': self.created_by.username,
                    'type': NEWS_TYPE_NEWS,
                }
                Notification.objects.create(**notification_data)

            # =======================================
            # Follow Up
            # =======================================
            elif template.type == NotificationTemplate.TYPE_NOTIFY_FOLLOW_UP:

                if not template.trigger_pattern:
                    continue

                delay_days = template.trigger_delay_days

                for i, tick in enumerate(template.trigger_pattern):

                    days = i+1

                    if bool(int(tick)):
                        follow_up_data = {
                            'report': self,
                            'template': template,
                            'authority': authority,
                            'date': created_on + datetime.timedelta(minutes=days*settings.MINUTES_PER_DAY),
                            'deadline': created_on + datetime.timedelta(minutes=(days+delay_days)*settings.MINUTES_PER_DAY)
                        }

                        FollowUp.objects.create(**follow_up_data)


    def create_comment_state(self):

        from reports.serializers import ReportCommentSerializer

        try:
            template_comment_state = Configuration.objects.get(system='web.template.report',
                                                               key='comment_state').value
        except Configuration.DoesNotExist:
            template_comment_state = u'@[%(username)s] ได้ทำการตั้งค่าสถานะเป็น %(state)s'



        comment_owner = self.updated_by or self.created_by
        message = template_comment_state % {'username': comment_owner.username, 'state': self.state_name}

        if self._state_changed_by_case:
            message += u' ด้วยเงื่อนไข : %s' % self._state_changed_by_case.description

            if self._state_changed_by_case._extra_info:
                message += u' โดยมีข้อมูลเพิ่มเติม: %s' % self._state_changed_by_case._extra_info

        comment = ReportComment.objects.create(
            report=self,
            message=message,
            created_by=comment_owner,
            state=self.state
        )

    def state_changed(self):
        return ((self.var_cache['state'] and self.state and self.var_cache['state'].id != self.state.id)
                or (not self.var_cache['state'] and self.state)
                or (self.var_cache['state'] and not self.state))

    def mask_responsed_follow_up(self):

        try:
            folow_up = FollowUp.objects.filter(report=self.parent, date__lte=self.created_at).latest('deadline')
            folow_up.responsed = True
            folow_up.save()

        except FollowUp.DoesNotExist:
            pass

    def clear_follow_up(self):
        FollowUp.objects.filter(report=self).update(disabled=True)

    def add_comment_to_parent(self):
        # if this is not a follow-up report then do nothing
        if not self.parent:
            return

        try:
            template_comment_followup = Configuration.objects.get(
                system='web.template.report',
                key='comment_followup'
            ).value
        except Configuration.DoesNotExist:
            template_comment_followup = u'''
                <p>มีรายงานติดตาม #%(report_id)s เข้ามาใหม่ โดยมีข้อมูลเบื้องต้นดังนี้</p>
                <p class="comment-rendered-form-data">%(rendered_form_data)s</p>
            '''

        comment_owner = self.created_by
        message = template_comment_followup % {
            'username': comment_owner.username,
            'report_id': self.id,
            'rendered_form_data': self.rendered_form_data
        }

        return ReportComment.objects.create(
            report=self.parent,
            message=message,
            created_by=comment_owner
        )

    def assign_administration_area(self):

        if self.is_public and not self.administration_area and self.report_location:

            domain_id = self.domain_id or self.created_by.domain_id or get_current_domain_id()

            # fix only public 'areas'
            admin_area = AdministrationArea.objects.filter(mpoly__covers=self.report_location, code__startswith='public_%s' % domain_id)
            if admin_area.count() > 0:
                self.administration_area = admin_area[0]
                self.administration_location = self.administration_area.location

            else:
                public_area = get_public_area()
                self.administration_area = public_area
                self.administration_location = self.administration_area.location

        elif self.administration_area and not self.administration_location:
            self.administration_location = self.administration_area.location

        # print self.administration_area, self.administration_location

    def get_publish_administration_area(self):
        if not self.is_public:

            if 'public_%s' % self.domain_id in self.administration_area.code:
                return self.administration_area

            report_location = self.report_location
            if not report_location:
                report_location = self.administration_area.location

            # fix only public 'areas'
            admin_area = AdministrationArea.objects.filter(mpoly__covers=report_location, code__startswith='public_%s' % self.domain_id)
            if admin_area.count() > 0:
                administration_area = admin_area[0]

            else:
                public_area = get_public_area()
                administration_area = public_area

            return administration_area
        else:
            return self.administration_area

    def get_public_feed_score(self):
        """Simply return score to set in public feed."""
        return calendar.timegm(self.created_at.utctimetuple())

    def get_public_feed_key(self):
        """Return specific public feed key for report.administration_area"""
        return get_public_feed_key(self.administration_area)

    def _add_to_public_feed(self, public_feed_key):
        score = self.get_public_feed_score()
        name = self.pk

        redis = get_redis_connection()
        ''':type : redis.client.StrictRedis'''
        redis.zadd(public_feed_key, score, name)

    def add_to_public_feed(self):
        """Add to public feed if report is a public one."""

        # If report is not saved or not a public report, do nothing.
        if not self.pk or not self.is_public:
            return

        public_feed_key = self.get_public_feed_key()
        self._add_to_public_feed(public_feed_key)

    def remove_from_public_feed(self):
        import copy
        """Remove from public feed if report is deleted"""
        name = self.id
        public_feed_key = self.get_public_feed_key()

        redis = get_redis_connection()
        ''':type : redis.client.StrictRedis'''
        redis.zrem(public_feed_key, name)

        # also remove from public feed area in which this report got curated in.
        for area in self.curated_in.all():
            curated_public_feed_key = get_public_feed_key(area)
            redis.zrem(curated_public_feed_key, name)

    def curate_in_administration_area(self, administration_area):
        """
        Select this report to be shown in specific administration_area

        :param administration_area
        """
        if not self.pk:
            return

        # Add in curated list first.
        self.curated_in.add(administration_area)

        public_feed_key = get_public_feed_key(administration_area)
        self._add_to_public_feed(public_feed_key)

    def update_plan_level_templates(self):
        for plan_report in self._plan_reports:
            plan_report.save()


    @app.task(filter=task_method, base=DomainTask, bind=True)
    @domain_celery_task
    def post_save(self):
        from reports.tasks import send_data_to_spreadsheet, send_data_to_calendar, delete_calendar_data, undelete_calendar_data
        from reports.serializers import ReportSerializer
        from notifications.models import NotificationTemplate

        unique, state_change_unique = ReportStateUnique.objects.get_or_create(report=self, state=self.state)

        if self.test_flag:
            publish_report(ReportSerializer(self).data)
            delete_calendar_data(self)
            return

        if self._test_flag != self.test_flag:
            undelete_calendar_data(self)

        self.add_to_public_feed()

        self.create_cep()
        if not self.parent:

            # When update state, don't send reporter feedback notification

            if self.state_changed():
                self.create_comment_state()

                if state_change_unique and self.negative:
                    self.create_reporter_notification([NotificationTemplate.TYPE_REPORTER_FEEDBACK])
                    # Check plans accepted
                    plan_accepted_list = self.accepted_plans()
                    self.create_report_notification(plan_accepted_list=plan_accepted_list)
                    self.create_comment_plans()
                    self.update_plan_level_templates()

                    send_data_to_calendar(self)

                self.clear_follow_up()
                self.create_reporter_notification([
                    NotificationTemplate.TYPE_NOTIFY_FOLLOW_UP,
                ])
                self.create_report_notification(types=[NotificationTemplate.TYPE_DELAYED_FOLLOW_UP])

                log_item = LogItem.objects.log_action(
                    key='REPORT_STATE_CHANGE',
                    object1=self,
                    object2=self.state,
                    created_by=self.updated_by or self.created_by,
                )

                from reports.serializers import ReportStateSerializer
                push_data = {
                    'reportId': log_item.object_id1,
                    'state': ReportStateSerializer(self.state).data,
                    'createdAt': log_item.created_at,
                    'createdBy': UserSerializer(log_item.created_by).data,
                }
                publish_report_state(push_data)
        else:

            # Force set follow up report state to default state
            self.state = self.type.default_state

            if self.is_new:
                self.parent.form_data = self.form_data
                self.parent.save()
                self.mask_responsed_follow_up()
                self.add_comment_to_parent()

        self._plan_accepted_list = None

        if self.is_new:
            send_data_to_spreadsheet(self)

        publish_report(ReportSerializer(self).data)

    def make_default_area(self):

        if not self.administration_area:

            try:
                self.administration_area = filter_permitted_administration_areas_and_descendants(self.created_by)[0]
            except IndexError:
                raise Exception("Default area not found. Please contact administrator for config area")

    def check_suspect_test_report(self):

        if self.id:
            return

        # dodd's reports should't be test.
        if self.is_public or self.created_by.is_public:
            self.test_flag = False
            return

        try:
            hours = Configuration.objects.get(system='web.report.suspect_test_after_join', key='hours').value
        except Configuration.DoesNotExist:
            hours = 0

        created_at = timezone.now()
        if created_at - self.created_by.date_joined < datetime.timedelta(hours=float(hours)):
            self.test_flag = True

    def save(self, *args, **kwargs):
        self._test_flag = self.var_cache.get('test_flag')

        # cleansing
        if settings.ADJUST_DATE and self.id is None:
            self.adjust_incident_date()

        # check #1: if this is a new report, do these things once
        #    1. save original data
        #    2. assign default state
        self.is_new = not self.id
        if self.is_new:
            self.original_form_data = self.form_data
            try:
                self.state = self.type.default_state
            except ReportState.DoesNotExist:
                pass
        # 3. protect wrong path when cep update state
        elif self._state_changed_by_case and self.var_cache['state'] and self._state_changed_by_case.from_state:
            if self.var_cache['state'].id != self._state_changed_by_case.from_state.id:
                raise Exception(u'in case %s not allowed change state from %s to %s' % (
                    self._state_changed_by_case.description,
                    self.var_cache['state'].name,
                    self._state_changed_by_case.to_state.name
                ))


        # evaluate pre_save custom script.
        report_pre_save = (self.type.report_pre_save or '').strip()
        if report_pre_save:
            symtable = {
                'report': self
            }
            safe_eval(report_pre_save, symtable)

        # check #2: if this report has test_flag = True then,
        #    1. mark this report as a positive(negative = False) one.
        #    2. do not change flag if client provided it.
        self.check_suspect_test_report()
        if self.test_flag:
            self.negative = False

        if self.created_by.is_anonymous:
            raise Exception('Anonymous can not create report')

        # check #3: find the most possible area for the report, if not found
        # try to assign the default area.
        self.is_public = self.created_by.is_public

        if self.is_public:

            if not self.report_location:
                raise Exception('Public report location required')

            self.negative = True

        self.assign_administration_area()
        self.make_default_area()
        super(Report, self).save(*args, **kwargs)

        # clear cache state_code
        self._state_code = None

        self.post_save.delay()

    def get_like(self, user=None):

        if not user:
            user = get_current_user()

        if not user:
            return None

        try:
            return ReportLike.objects.get(created_by=user.id, report=self, status=STATUS_PUBLISH)
        except ReportLike.DoesNotExist:
            return None

    def get_me_too(self, user=None):

        if not user:
            user = get_current_user()

        if not user:
            return None

        try:
            return ReportMeToo.objects.get(created_by=user.id, report=self, status=STATUS_PUBLISH)
        except ReportMeToo.DoesNotExist:
            return None

    def user_can_edit(self, user):

        # use in detail but maybe slow
        # extra = self.administration_area.authority.admins.filter(id=user.id).exist()

        return user_can_edit_basic_check(user, True)



    @property
    def rendered_report_footer(self):
        return render_to_string(
            'notifications/report_footer.html', {
                'report': self,
            })

    @property
    def rendered_case_footer(self):
        return render_to_string(
            'notifications/case_footer.html', {
                'report': self,
            })

    @property
    def rendered_report_compact(self):
        return render_to_string(
            'notifications/report_compact.html', {
                'report': self,
            })

    @property
    def rendered_case_compact(self):
        return render_to_string(
            'notifications/case_compact.html', {
                'report': self,
            })

    @property
    def rendered_report_subject(self):
        return render_to_string(
            'notifications/report_subject.html', {
                'report': self,
            })

    @property
    def rendered_case_subject(self):
        return render_to_string(
            'notifications/case_subject.html', {
                'report': self,
            })

    @property
    def rendered_data(self):
        template = self.type.django_template

        form_data = json.loads(self.form_data)
        t = Template(template)
        c = Context(form_data)

        return striptags(t.render(c))


class ReportImage(DomainMixin):
    report = models.ForeignKey('Report', related_name='images')
    guid = models.TextField()                                       # unique identifier
    note = models.TextField(blank=True)
    image_url = models.URLField()
    thumbnail_url = models.URLField()
    location = models.PointField(null=True, blank=True)


class ReportComment(DomainMixin):
    report = models.ForeignKey('Report', related_name='comments')
    message = models.TextField()
    file_url = models.TextField(null=True, blank=True)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='comment_created_by')
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='comment_updated_by', null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    state = models.ForeignKey(ReportState, related_name='report_comment_state', null=True, blank=True)
    status = models.IntegerField(max_length=100, choices=STATUS_CHOICES, default=STATUS_PUBLISH)

    def save(self, *args, **kwargs):
        is_new = self.id is None

        super(ReportComment, self).save(*args, **kwargs)

        split_mentionees = re.split(r"@\[(?P<username>[\w\-]+)\]", self.message)
        mentionees = User.objects.filter(username__in=split_mentionees)

        for mentionee in mentionees:
            mention = Mention.objects.create(
                comment=self,
                mentioner=self.created_by,
                mentionee=mentionee
            )
            if self.created_by == mentionee:
                mention.is_notified = True
                mention.save()

            mentionee_id = mention.mentionee.id
            serializer = MentionSerializer(mention)
            serializer.data['mentioneeId'] = mentionee_id
            publish_mention(serializer.data)

        from reports.serializers import ReportCommentSerializer
        
        if is_new:
            report = self.report
            Report.objects.filter(id=report.id).update(comment_count=report.comment_count + 1)
            invalidate_obj(report)

            serializer = ReportCommentSerializer(self)
            publish_comment(serializer.data)

    def delete(self, *args, **kwargs):
        report = Report.objects.get(id=self.report.id) # No cache

        Report.objects.filter(id=report.id).update(comment_count=report.comment_count - 1)
        invalidate_obj(report)

        super(ReportComment, self).delete(*args, **kwargs)

    def user_can_edit(self, user):
        return user == self.created_by


class ReportLike(AbstractCachedModel, DomainMixin):
    report = models.ForeignKey('Report', related_name='likes')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL)
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.IntegerField(max_length=100, choices=STATUS_CHOICES, default=STATUS_PUBLISH)


    class Meta:
        unique_together = (('report', 'created_by'),)

    def save(self, *args, **kwargs):
        is_new = self.id is None

        super(ReportLike, self).save(*args, **kwargs)

        if is_new or self.var_cache['status'] != self.status:
            report = self.report
            # value = -1 if self.status == STATUS_DELETE else 1
            # total_like_count = max(0, report.like_count + value)
            total_like_count = ReportLike.objects.filter(report=self.report, status=1).count()
            Report.objects.filter(id=self.report.id).update(like_count=total_like_count)
            invalidate_obj(report)

    def delete(self, *args, **kwargs):
        report = Report.objects.get(id=self.report.id)  # No cache
        #Report.objects.filter(id=self.report.id).update(like_count=report.like_count - 1)

        total_like_count = ReportLike.objects.filter(report=self.report, status=1).count()
        Report.objects.filter(id=self.report.id).update(like_count=total_like_count)

        self.status = STATUS_DELETE
        self.save()

    def remove(self, *args, **kwargs):
        self.delete(*args, **kwargs)
        super(ReportLike, self).delete(*args, **kwargs)


class ReportMeToo(AbstractCachedModel, DomainMixin):
    report = models.ForeignKey('Report', related_name='me_toos')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL)
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.IntegerField(max_length=100, choices=STATUS_CHOICES, default=STATUS_PUBLISH)

    class Meta:
        unique_together = (('report', 'created_by'),)

    def save(self, *args, **kwargs):
        is_new = self.id is None

        super(ReportMeToo, self).save(*args, **kwargs)

        if is_new or self.var_cache['status'] != self.status:
            report = self.report
            # value = -1 if self.status == STATUS_DELETE else 1
            # total_me_too = max(0, report.me_too_count + value)
            total_me_too = ReportMeToo.objects.filter(report=self.report, status=1).count()
            Report.objects.filter(id=self.report.id).update(me_too_count=total_me_too)
            invalidate_obj(report)

    def delete(self, *args, **kwargs):

        report = Report.objects.get(id=self.report.id) # No cache

        total_me_too = ReportMeToo.objects.filter(report=self.report, status=1).count()
        Report.objects.filter(id=self.report.id).update(me_too_count=total_me_too)

        #Report.objects.filter(id=self.report.id).update(me_too_count=report.me_too_count - 1)

        self.status = STATUS_DELETE
        self.save()


    def remove(self, *args, **kwargs):
        self.delete(*args, **kwargs)
        super(ReportMeToo, self).delete(*args, **kwargs)


class ReportAbuse(AbstractCachedModel, DomainMixin):
    report = models.ForeignKey('Report', related_name='report_abuses')
    reason = models.TextField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL)
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.IntegerField(max_length=100, choices=STATUS_CHOICES, default=STATUS_PUBLISH)

    class Meta:
        unique_together = (('report', 'created_by'),)


class ReportStateUnique(DomainMixin):
    report = models.ForeignKey('Report', related_name='report_state_unique_report')
    state = models.ForeignKey('ReportState', related_name='report_state_unique_state', null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = (('report', 'state'),)


class SpreadsheetResponse(DomainMixin):
    name = models.CharField(max_length=255)
    key = models.CharField(max_length=255)
    report_types = models.ManyToManyField('ReportType', related_name='reporttype_response', null=True, blank=True)
    # administration_areas = models.ManyToManyField('AdministrationArea', related_name='administrationarea_response', null=True, blank=True)
    authorities = models.ManyToManyField('accounts.Authority', related_name='authority_response', null=True, blank=True)

    def __unicode__(self):
        return "%s" % (self.name)


class GoogleCalendarResponse(DomainMixin):
    name = models.CharField(max_length=255)
    calendar_id = models.CharField(max_length=255)

    report_states = models.ManyToManyField('ReportState', related_name='reportstate_calendar_response', null=True, blank=True)
    authorities = models.ManyToManyField('accounts.Authority', related_name='authority_calendar_response', null=True, blank=True)
    render_template = models.TextField(null=True, blank=True)

    def __unicode__(self):
        return "%s" % (self.name)


class GoogleCalendarResponseEvent(DomainMixin):
    event_id = models.TextField()
    data = models.TextField()
    date = models.DateTimeField()
    deleted = models.BooleanField(default=False)

    calendar = models.ForeignKey('reports.GoogleCalendarResponse', related_name='calendar_events')
    report = models.ForeignKey('reports.Report', related_name='report_calendar_events')

    def __unicode__(self):
        return "%s" % (self.event_id)


class ReportInvestigation(DomainMixin):
    report = models.ForeignKey('Report', related_name='investigations')
    # parent = models.ForeignKey('self', null=True, blank=True)

    # type = models.PositiveIntegerField(default=1, choices=INVESTIGATION_TYPE)

    # form_data = models.TextField()
    note = models.TextField(blank=True)

    result = models.BooleanField(default=False)
    file = models.URLField(null=True, blank=True)

    investigation_date = models.DateField()

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='investigation_created_by')
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='investigation_updated_by', null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class AnimalLaboratoryCause(DomainMixin):
    name = models.TextField(unique=True)
    note = models.TextField(blank=True)

    def __unicode__(self):
        return "%s" % (self.name)


class ReportLaboratoryCase(DomainMixin):
    report = models.ForeignKey('Report', related_name='laboratory_results')

    case_no = models.TextField(unique=True)
    note = models.TextField(blank=True)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='laboratory_result_created_by')
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='laboratory_result_updated_by', null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class ReportLaboratoryItem(DomainMixin):
    case = models.ForeignKey('ReportLaboratoryCase', related_name='laboratory_items')

    sample_no = models.TextField(unique=True)
    positive_causes = models.ManyToManyField('AnimalLaboratoryCause', related_name="positive_causes", null=True, blank=True)
    negative_causes = models.ManyToManyField('AnimalLaboratoryCause', related_name="negative_causes", null=True, blank=True)

    note = models.TextField(blank=True)

    def __unicode__(self):
        return "%s" % (self.sample_no)

    @property
    def positive_causes_text(self):
        text = [cause.name for cause in self.positive_causes.all()]
        return ", ".join(text)

    @property
    def negative_causes_text(self):
        text = [cause.name for cause in self.negative_causes.all()]
        return ", ".join(text)


class ReportLaboratoryFile(DomainMixin):
    case = models.ForeignKey('ReportLaboratoryCase', related_name='laboratory_files')
    name = models.TextField()
    file = models.URLField()

    def __unicode__(self):
        return "%s-%s" % (self.case.id, self.file)
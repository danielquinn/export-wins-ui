import re

from datetime import datetime
from dateutil.relativedelta import relativedelta

from django import forms
from django.conf import settings
from django.core.urlresolvers import reverse

from alice.helpers import rabbit, get_form_field
from alice.metaclasses import ReflectiveFormMetaclass
from ui.forms import BootstrappedForm


class WinReflectiveFormMetaclass(ReflectiveFormMetaclass):

    reflection_url = settings.WINS_AP

    def __new__(mcs, name, bases, attrs):
        new_class = ReflectiveFormMetaclass.__new__(mcs, name, bases, attrs)

        make_typed_choice = (
            "is_prosperity_fund_related",
            "is_e_exported",
            "has_hvo_specialist_involvement",
        )
        for name in make_typed_choice:
            form_field = forms.TypedChoiceField(
                coerce=lambda x: x == "True",
                choices=((True, 'Yes'), (False, 'No')),
                widget=forms.RadioSelect,
                label=new_class._schema[name]["label"]
            )
            new_class.base_fields[name] = form_field
            new_class.declared_fields[name] = form_field

        return new_class


class ConfirmationFormMetaclass(ReflectiveFormMetaclass):

    reflection_url = settings.CONFIRMATIONS_AP

    def __new__(mcs, name, bases, attrs):

        new_class = ReflectiveFormMetaclass.__new__(mcs, name, bases, attrs)

        make_typed_choice = (
            "agree_with_win",
            "case_study_willing"
        )
        for name in make_typed_choice:
            form_field = forms.TypedChoiceField(
                coerce=lambda x: x == "True",
                choices=((True, 'Yes'), (False, 'No')),
                widget=forms.RadioSelect,
                label=new_class._schema[name]["label"]
            )
            new_class.base_fields[name] = form_field
            new_class.declared_fields[name] = form_field

        return new_class


class RabbitMixin(object):

    def push(self, url, data):
        """ POST data to URL on data server, return json response """

        # The POST request is http-url-encoded rather than json-encoded for now
        # since I don't know how to set it that way and don't have the time to
        # find out.
        response = rabbit.post(url, data=data, request=self.request)

        if not response.status_code == 201:
            raise forms.ValidationError(
                "Something has gone terribly wrong.  Please contact support.")

        return response.json()


class WinForm(RabbitMixin, BootstrappedForm,
              metaclass=WinReflectiveFormMetaclass):

    # We're only caring about MM/YYYY formatted dates
    date = forms.fields.CharField(max_length=7, label="Date business won")

    class Meta(object):
        exclude = ("id", "user")

    def __init__(self, *args, **kwargs):

        self.request = kwargs.pop("request")

        BootstrappedForm.__init__(self, *args, **kwargs)

        self.date_format = 'MM/YYYY'  # the format the date field expects

        self.fields["date"].widget.attrs.update({"placeholder": self.date_format})

        self.fields["is_personally_confirmed"].required = True
        self.fields["is_personally_confirmed"].label_suffix = ""

        self.fields["is_line_manager_confirmed"].required = True
        self.fields["is_line_manager_confirmed"].label_suffix = ""

        self.fields["total_expected_export_value"].widget.attrs.update(
            {"placeholder": "£GBP"})
        self.fields["total_expected_non_export_value"].widget.attrs.update(
            {"placeholder": "£GBP"})
        self.fields["total_expected_export_value"].initial = '0'
        self.fields["total_expected_non_export_value"].initial = '0'

        self._add_breakdown_fields()

        self.advisor_fields = self._get_advisor_fields()
        self._add_advisor_fields()

    def clean_date(self):
        """ Validate date entered as a string and reformat for serializer """

        date_str = self.cleaned_data.get("date")

        m = re.match(r"^(?P<month>\d\d)/(?P<year>\d\d\d\d)$", date_str)
        if not m:
            raise forms.ValidationError(
                'Invalid format. Please use {}'.format(self.date_format))

        try:
            year = int(m.group("year"))
            month = int(m.group("month"))
            if year < 1970:
                raise ValueError("Year is unreasonable")
            date = datetime(year, month, 1)
        except:
            raise forms.ValidationError(
                'Invalid date. Please use {}'.format(self.date_format))

        return date.strftime('%Y-%m-%d')  # serializer expects YYYY-MM-DD

    def clean_is_personally_confirmed(self):
        r = self.cleaned_data.get("is_personally_confirmed")
        if not r:
            raise forms.ValidationError("This is a required field")
        return r

    def clean_is_line_manager_confirmed(self):
        r = self.cleaned_data.get("is_line_manager_confirmed")
        if not r:
            raise forms.ValidationError("This is a required field")
        return r

    def save(self):
        """ Push cleaned data to appropriate data server access points """

        # This doesn't really matter, since the data server ignores this value
        # and substitutes the logged-in user id.  However, if you don't provide
        # it, the serialiser explodes, so we attach it for kicks.
        self.cleaned_data["user"] = self.request.user.pk

        win = self.push(settings.WINS_AP, self.cleaned_data)

        for data in self._get_breakdown_data(win["id"]):
            self.push(settings.BREAKDOWNS_AP, data)

        for data in self._get_advisor_data(win["id"]):
            self.push(settings.ADVISORS_AP, data)

        self.send_notifications(win["id"])

    def send_notifications(self, win_id):
        """
        Tell the data server to send mail. Failures will not blow up at the
        client, but will blow up the server, so we'll be notified if something
        goes wrong.
        """

        # tell data server to send officer notification
        # initially concieved to tell officer that customer notifciation has
        # been sent, but since that is currently manually managed, we in fact
        # only send an intermediate email letting them know that someday we will
        # send the customer an email (later by manual process)
        rabbit.post(settings.NOTIFICATIONS_AP, data={
            "win": win_id,
            "type": "o",  # officer notification
            "user": self.request.user.pk,
        })

        # tell data sever to send customer notification
        # currently commented out and handled manually by management command
        # in data server and gino
        # rabbit.post(settings.NOTIFICATIONS_AP, data={
        #     "win": win_id,
        #     "type": "c",  # customer notification
        #     "recipient": self.cleaned_data["customer_email_address"],
        #     "url": self.request.build_absolute_uri(
        #         reverse("responses", kwargs={"pk": win_id})
        #     )
        # })

    def _add_breakdown_fields(self):
        """ Create breakdown fields """

        breakdown_values = ("breakdown_exports_{}", "breakdown_non_exports_{}")

        now = datetime.utcnow()

        for i in range(0, 5):

            d = now + relativedelta(years=i)

            for field in breakdown_values:
                self.fields[field.format(i)] = forms.fields.IntegerField(
                    label="{}/{}".format(d.year, str(d.year + 1)[-2:]),
                    widget=forms.fields.NumberInput(
                        attrs={
                            "class": "form-control",
                            "placeholder": "£GBP"
                        }
                    ),
                    initial='0',
                    max_value=2000000000,
                    label_suffix=""
                )

    def _get_breakdown_data(self, win_id):
        """ Make breakdown data for pushing to endpoint from cleaned data """

        r = []
        now = datetime.utcnow()

        for i in range(0, 5):
            d = now + relativedelta(years=i)
            for t in ("exports", "non_exports"):
                value = self.cleaned_data.get("breakdown_{}_{}".format(t, i))
                if value:
                    r.append({
                        "type": "1" if t == "exports" else "2",
                        "year": d.year,
                        "value": value,
                        "win": win_id
                    })
        return r

    def _get_advisor_fields(self):
        """ Make list of lists of advisor field names and specs """

        advisor_schema = rabbit.get(settings.ADVISORS_AP + "schema/").json()
        advisor_fields = []
        num_advisor_fields = 5
        for i in range(0, num_advisor_fields):
            instance_fields = []
            for name, spec in advisor_schema.items():
                if name == 'win':
                    # don't want a field for the foreign key in the form
                    continue
                field_name = "advisor_{}_{}".format(i, name)
                instance_fields.append((name, field_name, spec))
            advisor_fields.append(instance_fields)
        return advisor_fields

    def _add_advisor_fields(self):
        """ Create advisor fields from advisor data """

        for instance_data in self.advisor_fields:
            for _, field_name, spec in instance_data:
                self.fields[field_name] = get_form_field(spec)
                self.fields[field_name].required = False
                self.fields[field_name].widget.attrs.update({
                    "class": "form-control"
                })

    def _get_advisor_data(self, win_id):
        """ Make advisor data for pushing to endpoint from cleaned data """

        advisor_data = []
        for instance_data in self.advisor_fields:
            instance_dict = {
                name: self.cleaned_data[field_name]
                for name, field_name, _ in instance_data
            }
            if not instance_dict['name']:
                # ignore any instances where user has not input a name
                continue
            # manually add the foriegn key for the newly created win
            instance_dict['win'] = win_id
            advisor_data.append(instance_dict)
        return advisor_data


class ConfirmationForm(RabbitMixin, BootstrappedForm,
                       metaclass=ConfirmationFormMetaclass):

    win = forms.CharField(max_length=128)

    def __init__(self, *args, **kwargs):

        self.request = kwargs.pop("request")

        BootstrappedForm.__init__(self, *args, **kwargs)

        self.fields["win"].widget = forms.widgets.HiddenInput()

    def send_notifications(self, win_id):
        pass

    def save(self):

        confirmation = self.push(settings.CONFIRMATIONS_AP, self.cleaned_data)

        self.send_notifications(confirmation)

from django import forms
from django.utils.translation import gettext_lazy as _


class TenantLoginForm(forms.Form):
    tenant_code = forms.SlugField(
        max_length=64,
        widget=forms.TextInput(
            attrs={
                "class": "w-full px-4 py-3 rounded-xl bg-white/50 border border-slate-200 focus:border-blue-500 focus:ring-2 focus:ring-blue-200 outline-none transition-all placeholder:text-slate-400 font-medium text-slate-700",
                "placeholder": "e.g. al-nour",
                "autocomplete": "organization",
                "autocapitalize": "off",
                "spellcheck": "false",
            }
        ),
        label=_("Company Code"),
    )
    username = forms.CharField(
        max_length=150,
        widget=forms.TextInput(
            attrs={
                "class": "w-full px-4 py-3 rounded-xl bg-white/50 border border-slate-200 focus:border-blue-500 focus:ring-2 focus:ring-blue-200 outline-none transition-all placeholder:text-slate-400 font-medium text-slate-700",
                "placeholder": _("Enter your username"),
                "autocomplete": "username",
                "autocapitalize": "off",
                "spellcheck": "false",
            }
        ),
        label=_("Username / Email"),
    )
    password = forms.CharField(
        strip=False,
        widget=forms.PasswordInput(
            attrs={
                "class": "w-full px-4 py-3 rounded-xl bg-white/50 border border-slate-200 focus:border-blue-500 focus:ring-2 focus:ring-blue-200 outline-none transition-all placeholder:text-slate-400 font-medium text-slate-700",
                "placeholder": "********",
                "autocomplete": "current-password",
            }
        ),
        label=_("Password"),
    )
    remember_me = forms.BooleanField(required=False, label=_("Remember me"))

    def clean_tenant_code(self):
        return (self.cleaned_data.get("tenant_code") or "").strip().lower()

    def clean_username(self):
        return (self.cleaned_data.get("username") or "").strip()


class OrganizationBootstrapForm(forms.Form):
    ENVIRONMENT_CHOICES = (
        ("dev", _("Dev")),
        ("demo", _("Demo")),
        ("test", _("Test")),
        ("live", _("Live")),
    )

    company_name = forms.CharField(
        max_length=200,
        widget=forms.TextInput(
            attrs={
                "class": "w-full rounded-2xl border border-slate-200 bg-white px-4 py-3 text-sm font-medium text-slate-800 outline-none transition focus:border-cyan-500 focus:ring-2 focus:ring-cyan-100",
                "placeholder": _("Company name"),
                "autocomplete": "organization-title",
            }
        ),
        label=_("Company Name"),
    )
    company_code = forms.SlugField(
        max_length=64,
        widget=forms.TextInput(
            attrs={
                "class": "w-full rounded-2xl border border-slate-200 bg-white px-4 py-3 text-sm font-medium text-slate-800 outline-none transition focus:border-cyan-500 focus:ring-2 focus:ring-cyan-100",
                "placeholder": _("company-code"),
                "autocapitalize": "off",
                "spellcheck": "false",
            }
        ),
        label=_("Company Code"),
    )
    owner_email = forms.EmailField(
        widget=forms.EmailInput(
            attrs={
                "class": "w-full rounded-2xl border border-slate-200 bg-white px-4 py-3 text-sm font-medium text-slate-800 outline-none transition focus:border-cyan-500 focus:ring-2 focus:ring-cyan-100",
                "placeholder": _("owner@company.com"),
                "autocomplete": "email",
            }
        ),
        label=_("Owner Email"),
    )
    owner_password = forms.CharField(
        strip=False,
        widget=forms.PasswordInput(
            attrs={
                "class": "w-full rounded-2xl border border-slate-200 bg-white px-4 py-3 text-sm font-medium text-slate-800 outline-none transition focus:border-cyan-500 focus:ring-2 focus:ring-cyan-100",
                "placeholder": _("Strong password"),
                "autocomplete": "new-password",
            }
        ),
        label=_("Owner Password"),
    )
    subscription_plan = forms.ChoiceField(
        choices=(
            ("free_trial", _("Free Trial")),
            ("pro", _("Pro")),
            ("enterprise", _("Enterprise")),
        ),
        initial="free_trial",
        widget=forms.Select(
            attrs={
                "class": "w-full rounded-2xl border border-slate-200 bg-white px-4 py-3 text-sm font-medium text-slate-800 outline-none transition focus:border-cyan-500 focus:ring-2 focus:ring-cyan-100",
            }
        ),
        label=_("Subscription Plan"),
    )
    environments = forms.MultipleChoiceField(
        choices=ENVIRONMENT_CHOICES,
        initial=["dev", "demo"],
        widget=forms.CheckboxSelectMultiple,
        label=_("Environments"),
    )
    demo_password = forms.CharField(
        required=False,
        initial="DemoPass123!",
        widget=forms.TextInput(
            attrs={
                "class": "w-full rounded-2xl border border-slate-200 bg-white px-4 py-3 text-sm font-medium text-slate-800 outline-none transition focus:border-cyan-500 focus:ring-2 focus:ring-cyan-100",
                "placeholder": _("Shared demo password"),
                "autocomplete": "off",
            }
        ),
        label=_("Demo Seed Password"),
        help_text=_("Used only for the seeded role-based demo users."),
    )

    def clean_company_code(self):
        return (self.cleaned_data.get("company_code") or "").strip().lower()

    def clean_owner_email(self):
        return (self.cleaned_data.get("owner_email") or "").strip().lower()

    def clean_environments(self):
        values = self.cleaned_data.get("environments") or []
        normalized = []
        for value in values:
            item = (value or "").strip().lower()
            if item and item not in normalized:
                normalized.append(item)
        if not normalized:
            raise forms.ValidationError(_("Select at least one environment."))
        return normalized

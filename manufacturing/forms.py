from django import forms
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.utils.text import slugify

from .models import ProductionLog, WorkOrder
from tenancy.models import Organization, Tenant


class WorkOrderForm(forms.ModelForm):
    class Meta:
        model = WorkOrder
        fields = [
            "customer",
            "bom",
            "quantity",
            "due_date",
            "priority",
        ]
        widgets = {
            "customer": forms.Select(attrs={"class": "w-full border border-gray-300 rounded-lg p-2 focus:ring focus:ring-blue-300 transition"}),
            "bom": forms.Select(attrs={"class": "w-full border border-gray-300 rounded-lg p-2 focus:ring focus:ring-blue-300 transition"}),
            "quantity": forms.NumberInput(attrs={"class": "w-full border border-gray-300 rounded-lg p-2 focus:ring focus:ring-blue-300 transition"}),
            "due_date": forms.DateTimeInput(
                attrs={"class": "w-full border border-gray-300 rounded-lg p-2 focus:ring focus:ring-blue-300 transition", "type": "datetime-local"}
            ),
            "priority": forms.Select(attrs={"class": "w-full border border-gray-300 rounded-lg p-2 focus:ring focus:ring-blue-300 transition"}),
        }

    def clean_bom(self):
        bom = self.cleaned_data.get("bom")
        if bom and bom.status != "active":
            raise forms.ValidationError("Work Orders can only be created from Active BOMs.")
        return bom


class ProductionLogForm(forms.ModelForm):
    class Meta:
        model = ProductionLog
        fields = ["work_order", "quantity", "shift", "note"]
        widgets = {
            "work_order": forms.Select(attrs={"class": "w-full p-2 border rounded-md"}),
            "quantity": forms.NumberInput(attrs={"class": "w-full p-2 border rounded-md"}),
            "shift": forms.Select(attrs={"class": "w-full p-2 border rounded-md"}),
            "note": forms.Textarea(attrs={"class": "w-full p-2 border rounded-md", "rows": 3}),
        }


class BulkImportForm(forms.Form):
    import_type = forms.ChoiceField(
        choices=[
            ("products", "Products"),
            ("machines", "Machines"),
            ("stages", "Production Stages"),
            ("employees", "Employees"),
            ("bom", "Bill of Materials"),
            ("work_orders", "Work Orders"),
        ],
        widget=forms.Select(attrs={"class": "w-full p-2 border rounded-md"}),
    )
    file = forms.FileField(widget=forms.FileInput(attrs={"class": "w-full p-2 border rounded-md"}))


class CompanyRegistrationForm(forms.Form):
    company_name = forms.CharField(
        max_length=200,
        widget=forms.TextInput(
            attrs={
                "class": "w-full p-3 border rounded-lg",
                "placeholder": "e.g. Acme Corp",
                "autocomplete": "organization",
            }
        ),
    )
    company_code = forms.SlugField(
        max_length=64,
        widget=forms.TextInput(
            attrs={
                "class": "w-full p-3 border rounded-lg",
                "placeholder": "e.g. al-nour",
                "autocomplete": "organization",
                "autocapitalize": "off",
                "spellcheck": "false",
            }
        ),
    )
    owner_email = forms.EmailField(
        widget=forms.EmailInput(
            attrs={
                "class": "w-full p-3 border rounded-lg",
                "placeholder": "owner@acme.com",
                "autocomplete": "email",
                "autocapitalize": "off",
                "spellcheck": "false",
            }
        )
    )
    owner_password = forms.CharField(
        min_length=8,
        widget=forms.PasswordInput(
            attrs={
                "class": "w-full p-3 border rounded-lg",
                "placeholder": "********",
                "autocomplete": "new-password",
            }
        ),
    )

    def clean_company_name(self):
        return (self.cleaned_data.get("company_name") or "").strip()

    def clean_company_code(self):
        raw_value = (self.cleaned_data.get("company_code") or "").strip().lower()
        code = slugify(raw_value)
        if not code:
            raise ValidationError("Enter a valid company code using letters, numbers, and hyphens.")
        if Organization.objects.using("default").filter(slug=code).exists() or Tenant.objects.using("default").filter(
            code=code,
            organization__isnull=True,
        ).exists():
            raise ValidationError("This company code is already in use. Please choose another.")
        return code

    def clean_owner_email(self):
        email = (self.cleaned_data.get("owner_email") or "").strip().lower()
        if Organization.objects.using("default").filter(owner_email__iexact=email).exists() or Tenant.objects.using("default").filter(
            owner_email__iexact=email,
            organization__isnull=True,
        ).exists():
            raise ValidationError("This owner email is already used by another company.")
        return email

    def clean_owner_password(self):
        password = self.cleaned_data.get("owner_password") or ""
        validate_password(password)
        return password

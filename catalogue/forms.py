from django import forms

from core.errors import RuleViolation
from core.forms import BootstrapFormMixin

from . import services
from .models import Acquisition, BookCopy, BookEdition, Category, LabelLayout


class EditionForm(BootstrapFormMixin, forms.ModelForm):
    isbn_acknowledged = forms.BooleanField(
        required=False, label="Another edition has this ISBN: continue anyway"
    )

    class Meta:
        model = BookEdition
        fields = [
            "title",
            "author",
            "publisher",
            "edition_label",
            "publication_year",
            "category",
            "isbn",
            "description",
            "notes",
        ]
        help_texts = {"author": "Leave blank or enter 'Unknown author'."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = Category.objects.filter(is_active=True)
        self.show_isbn_ack = False

    def clean(self):
        data = super().clean()
        try:
            isbn = services.clean_isbn(data.get("isbn", ""))
        except RuleViolation as exc:
            self.add_error("isbn", exc.message)
            return data
        data["isbn"] = isbn
        dup = services.find_isbn_duplicates(isbn, exclude_pk=self.instance.pk)
        if dup.exists() and not data.get("isbn_acknowledged"):
            self.show_isbn_ack = True
            self.add_error(
                "isbn", "Warning: another edition has this ISBN. Tick the box to continue anyway."
            )
        year = data.get("publication_year")
        try:
            services._check_year(year)
        except RuleViolation as exc:
            self.add_error("publication_year", exc.message)
        return data


class CopiesForm(BootstrapFormMixin, forms.Form):
    count = forms.IntegerField(min_value=1, max_value=200, initial=1)
    source = forms.ChoiceField(choices=Acquisition.Source.choices)
    donor_name = forms.CharField(required=False, max_length=200)
    acquired_on = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    unit_cost_rupees = forms.DecimalField(
        required=False, min_value=0, decimal_places=2, label="Unit cost (₹)"
    )
    shelf_location = forms.CharField(required=False, max_length=80)
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))


class CopyStateForm(BootstrapFormMixin, forms.Form):
    condition = forms.ChoiceField(choices=BookCopy.Condition.choices)
    status = forms.ChoiceField(choices=[c for c in BookCopy.Status.choices if c[0] != "borrowed"])
    note = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def clean(self):
        data = super().clean()
        if data.get("condition") and data.get("status"):
            try:
                services.validate_state(data["condition"], data["status"])
            except RuleViolation as exc:
                raise forms.ValidationError(exc.message) from None
        return data


class CategoryForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Category
        fields = ["name", "is_active"]

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        qs = Category.objects.filter(name__iexact=name).exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("A category with this name already exists.")
        return name


class LabelLayoutForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = LabelLayout
        fields = [
            "name",
            "page_width_mm",
            "page_height_mm",
            "label_width_mm",
            "label_height_mm",
            "columns",
            "rows",
            "margin_mm",
            "is_default",
        ]

    def clean(self):
        data = super().clean()
        if all(k in data for k in ("page_width_mm", "label_width_mm", "columns", "rows")):
            probe = LabelLayout(
                **{k: data[k] for k in self.Meta.fields if k in data and k != "name"}
            )
            try:
                from .labels import validate_layout

                validate_layout(probe)
            except RuleViolation as exc:
                raise forms.ValidationError(exc.message) from None
        return data

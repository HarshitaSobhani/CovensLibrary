from django import forms

CHECK_WIDGETS = (forms.CheckboxInput, forms.RadioSelect, forms.CheckboxSelectMultiple)


class BootstrapFormMixin:
    """Adds Bootstrap classes to widgets so forms look consistent."""

    def _style_widgets(self):
        for field in self.fields.values():
            w = field.widget
            if isinstance(w, CHECK_WIDGETS):
                w.attrs.setdefault("class", "form-check-input")
            elif isinstance(w, (forms.Select, forms.SelectMultiple)):
                w.attrs.setdefault("class", "form-select")
            elif not isinstance(w, forms.FileInput):
                w.attrs.setdefault("class", "form-control")
            else:
                w.attrs.setdefault("class", "form-control")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_widgets()

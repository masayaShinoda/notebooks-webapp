from django import forms

from .models import NOTEBOOK_COLORS, Notebook


class NotebookForm(forms.ModelForm):
    # `initial` so a radio is pre-checked on render -- otherwise a submit
    # without explicitly clicking a swatch sends no value at all for a
    # required field, and fails validation with no visible reason.
    color = forms.ChoiceField(choices=NOTEBOOK_COLORS, widget=forms.RadioSelect, initial='slate')

    class Meta:
        model = Notebook
        fields = ['title', 'color']


class NoteForm(forms.Form):
    """Not a ModelForm: `encrypted_content` should never be bound directly
    to a form field. `body` is plaintext in memory only, for the duration
    of the request -- views are responsible for calling Note.set_content().
    """

    title = forms.CharField(
        max_length=200, required=False,
        widget=forms.TextInput(attrs={
            'placeholder': 'Untitled note',
            # No visible <label> in the editor -- only the placeholder -- so name
            # the field for assistive tech.
            'aria-label': 'Note title',
            # Elevation transition is inherited from the @layer base input rule
            # (theme/static_src/app.css) so it eases in and out symmetrically.
            # Padding is left to that base rule too -- constant, no focus-time
            # shift, and it lines up with the body text below. The base rule's
            # 2px :focus outline is kept (no `focus:outline-none`) so keyboard
            # focus is visible -- `shadow-pressed` alone is too faint in dark mode.
            'class': 'text-scale-2 font-semibold border-none shadow-none bg-transparent rounded-lg focus:shadow-pressed max-lg:px-0',
        }),
    )
    body = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={
            'placeholder': 'Start writing in Markdown…',
            'aria-label': 'Note body (Markdown)',
            'class': 'h-full resize-none border-none focus:shadow-pressed '
                     'max-lg:shadow-none max-lg:focus:shadow-none max-lg:px-0 '
                     'max-lg:focus:outline-none',
        }),
    )
    # The Note.version the editor was loaded at (see note_save). Not required
    # at the form level: a missing value is a conflict, not a validation error,
    # so a page cached from before this field existed can't overwrite anything.
    base_version = forms.IntegerField(required=False, min_value=1, widget=forms.HiddenInput)

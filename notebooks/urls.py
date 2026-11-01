from django.urls import path

from . import views

app_name = 'notebooks'

urlpatterns = [
    path('', views.workspace, name='workspace'),
    path('notebooks/new/', views.notebook_create, name='notebook_create'),
    path('notebooks/<int:notebook_id>/', views.notebook_detail, name='notebook_detail'),
    path('notebooks/<int:notebook_id>/edit/', views.notebook_update, name='notebook_update'),
    path('notebooks/<int:notebook_id>/delete/', views.notebook_delete, name='notebook_delete'),
    path('notebooks/<int:notebook_id>/notes/new/', views.note_create, name='note_create'),
    path('notebooks/<int:notebook_id>/notes/<int:note_id>/', views.note_detail, name='note_detail'),
    path('notebooks/<int:notebook_id>/notes/<int:note_id>/save/', views.note_save, name='note_save'),
    path('notebooks/<int:notebook_id>/notes/<int:note_id>/version/', views.note_version, name='note_version'),
    path('notebooks/<int:notebook_id>/notes/<int:note_id>/preview/', views.note_preview, name='note_preview'),
    path('notebooks/<int:notebook_id>/notes/<int:note_id>/delete/', views.note_delete, name='note_delete'),
    path('notebooks/<int:notebook_id>/notes/<int:note_id>/history/', views.note_history, name='note_history'),
    path('notebooks/<int:notebook_id>/notes/<int:note_id>/history/<int:revision_id>/', views.note_history, name='note_revision'),
    path('notebooks/<int:notebook_id>/notes/<int:note_id>/history/<int:revision_id>/restore/', views.note_restore, name='note_restore'),
    path('trash/', views.trash, name='trash'),
    path('trash/empty/', views.trash_empty, name='trash_empty'),
    path('trash/notes/<int:note_id>/', views.trash_note, name='trash_note'),
    path('trash/notes/<int:note_id>/restore/', views.trash_restore_note, name='trash_restore_note'),
    path('trash/notes/<int:note_id>/delete/', views.trash_delete_note, name='trash_delete_note'),
    path('trash/notebooks/<int:notebook_id>/restore/', views.trash_restore_notebook, name='trash_restore_notebook'),
    path('trash/notebooks/<int:notebook_id>/delete/', views.trash_delete_notebook, name='trash_delete_notebook'),
]

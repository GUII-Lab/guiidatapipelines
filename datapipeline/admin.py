from django.contrib import admin
from .models import *

# Register your models here.
admin.site.register(Message)
admin.site.register(User)
admin.site.register(CustomGPT)
admin.site.register(FireData)
admin.site.register(FeedbackMessage)
admin.site.register(FeedbackGPT)
admin.site.register(Image)


@admin.register(FormSchema)
class FormSchemaAdmin(admin.ModelAdmin):
    list_display = ('schema_id', 'title', 'course_label', 'week_number', 'version', 'is_active', 'updated_at')
    list_filter = ('is_active', 'course_label', 'week_number')
    search_fields = ('schema_id', 'title', 'course_label')
    readonly_fields = ('created_at', 'updated_at')
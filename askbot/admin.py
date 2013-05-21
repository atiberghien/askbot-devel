# -*- coding: utf-8 -*-
"""
:synopsis: connector to standard Django admin interface

To make more models accessible in the Django admin interface, add more classes subclassing ``django.contrib.admin.Model``

Names of the classes must be like `SomeModelAdmin`, where `SomeModel` must 
exactly match name of the model used in the project
"""
from django.contrib import admin
from askbot import models

class EmailFeedSettingAdmin(admin.ModelAdmin):
    list_display = ('subscriber', 'feed_type', 'frequency', 'added_at', 'reported_at')
    list_editable = ('frequency',)
    list_filter = ('feed_type', 'frequency')
    ordering = ['subscriber']
    search_fields = ['subscriber__username']
    

class AnonymousQuestionAdmin(admin.ModelAdmin):
    """AnonymousQuestion admin class"""

class TagAdmin(admin.ModelAdmin):
    """Tag admin class"""

class VoteAdmin(admin.ModelAdmin):
    """  admin class"""

class FavoriteQuestionAdmin(admin.ModelAdmin):
    
    """  admin class"""

class PostRevisionAdmin(admin.ModelAdmin):
    """  admin class"""

class AwardAdmin(admin.ModelAdmin):
    """  admin class"""

class ReputeAdmin(admin.ModelAdmin):
    """  admin class"""

class ActivityAdmin(admin.ModelAdmin):
    """  admin class"""
    
class ThreadAdmin(admin.ModelAdmin):
    list_display = ('title', 'language_code', 'site', 'is_specific')
    list_filter = ('language_code', 'site', 'is_specific')
    ordering = ['title']
    search_fields = ['title']
    
admin.site.register(models.Thread, ThreadAdmin)
admin.site.register(models.Post)
admin.site.register(models.Tag, TagAdmin)
admin.site.register(models.Vote, VoteAdmin)
admin.site.register(models.FavoriteQuestion, FavoriteQuestionAdmin)
admin.site.register(models.PostRevision, PostRevisionAdmin)
admin.site.register(models.Award, AwardAdmin)
admin.site.register(models.Repute, ReputeAdmin)
admin.site.register(models.Activity, ActivityAdmin)
admin.site.register(models.EmailFeedSetting, EmailFeedSettingAdmin)

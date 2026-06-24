from django.contrib import admin
from .models import Group, GroupMember, GroupAdminPermission

admin.site.register(Group)
admin.site.register(GroupMember)
admin.site.register(GroupAdminPermission)

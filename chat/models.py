from django.db import models
from django.contrib.auth.models import AbstractUser


class CustomUser(AbstractUser):
    id_user = models.AutoField(primary_key=True)
    token = models.CharField(max_length=100, null=True, blank=True)
    token_expiration = models.DateTimeField(null=True, blank=True)
    dt_create = models.DateTimeField(auto_now_add=True)
    dt_last_login = models.DateTimeField(null=True, auto_now=True)
    # Avoid conflicts with Django's default User model
    groups = models.ManyToManyField(
        "auth.Group", related_name="customuser_groups", blank=True  # Unique related name
    )
    user_permissions = models.ManyToManyField(
        "auth.Permission", related_name="customuser_permissions", blank=True  # Unique related name
    )


class ChatGroup(models.Model):
    id_chat_group = models.AutoField(primary_key=True)
    st_chat_group_name = models.CharField(max_length=100)
    bl_personal = models.BooleanField(blank=False)
    id_owner = models.IntegerField()
    dt_create = models.DateTimeField(auto_now_add=True)


class ChatGroupMember(models.Model):
    id_chat_group = models.IntegerField()
    id_user = models.IntegerField()
    dt_join = models.DateTimeField(auto_now_add=True)
    dt_last_read = models.DateTimeField()


class ChatGroupMessages(models.Model):
    id_message = models.AutoField(primary_key=True)
    id_chat_group = models.IntegerField()
    id_sender = models.IntegerField()
    st_sender_name = models.CharField(max_length=100)
    tx_message_body = models.TextField()
    dt_create = models.DateTimeField(auto_now_add=True)

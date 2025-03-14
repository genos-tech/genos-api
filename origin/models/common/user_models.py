import uuid
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models


class CustomUserManager(BaseUserManager):
    def create_user(self, email, username, password=None, **extra_fields):
        """Creates and returns a user with an email and username"""
        if not email:
            raise ValueError("The Email field must be set")
        email = self.normalize_email(email)
        user = self.model(email=email, username=username, **extra_fields)
        user.set_password(password)  # Hash the password
        user.save(using=self._db)
        return user

    def create_superuser(self, email, username, password=None, **extra_fields):
        """Creates and returns a superuser"""
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        return self.create_user(email, username, password, **extra_fields)


class CustomUser(AbstractBaseUser, PermissionsMixin):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    username = models.CharField(max_length=50, unique=True)
    email = models.EmailField(unique=True)
    profile_image_url = models.URLField(blank=True, null=True)
    status = models.CharField(
        max_length=20,
        choices=[("online", "Online"), ("offline", "Offline"), ("away", "Away")],
        default="offline",
    )
    last_seen = models.DateTimeField(auto_now=True)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    token = models.CharField(max_length=100, null=True, blank=True)
    token_expiration = models.DateTimeField(null=True, blank=True)
    ts_last_login_at = models.DateTimeField(null=True, auto_now=True)
    # Avoid conflicts with Django's default User model
    groups = models.ManyToManyField(
        "auth.Group", related_name="customuser_groups", blank=True  # Unique related name
    )
    user_permissions = models.ManyToManyField(
        "auth.Permission", related_name="customuser_permissions", blank=True  # Unique related name
    )

    # Django Auth Fields
    is_active = models.BooleanField(default=True)  # Can be disabled
    is_staff = models.BooleanField(default=False)  # Access to admin panel

    objects = CustomUserManager()

    USERNAME_FIELD = "email"  # Use email as the unique identifier
    REQUIRED_FIELDS = ["username"]


class ChatGroup(models.Model):
    id_chat_group = models.AutoField(primary_key=True)
    st_chat_group_name = models.CharField(max_length=100)
    bl_personal = models.BooleanField(blank=False)
    id_owner = models.IntegerField()
    dt_create = models.DateTimeField(auto_now_add=True)


class ChatGroupMember(models.Model):
    id_chat_group = models.IntegerField(blank=False)
    id_user = models.IntegerField(blank=False)
    dt_join = models.DateTimeField(auto_now_add=True)
    dt_last_read = models.DateTimeField()


class ChatGroupMessages(models.Model):
    id_message = models.AutoField(primary_key=True)
    id_chat_group = models.IntegerField()
    id_sender = models.IntegerField()
    st_sender_name = models.CharField(max_length=100)
    tx_message_body = models.TextField()
    dt_create = models.DateTimeField(auto_now_add=True)

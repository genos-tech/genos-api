import os

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


def user_profile_image_path(instance, filename):
    return os.path.join(
        "user_profiles",
        str(instance.id),
        filename,
    )


class CustomUser(AbstractBaseUser, PermissionsMixin):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    username = models.CharField(max_length=50, unique=False)
    email = models.EmailField(unique=True)
    phone_number = models.CharField(max_length=20, blank=True, null=True)
    profile_image_url = models.FileField(upload_to=user_profile_image_path)
    profile_image_file_name = models.CharField(blank=True, null=True)
    is_offline_forced = models.BooleanField(default=False)
    custom_status = models.CharField(max_length=50, blank=True, null=True)
    role = models.CharField(max_length=50, blank=True, null=True)
    base_country = models.CharField(max_length=50, blank=True, null=True)
    last_seen = models.DateTimeField(auto_now=True)
    is_deleted = models.BooleanField(default=False)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    is_system_user = models.BooleanField(default=False)

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

    is_demo = models.BooleanField(default=False, db_index=True)

    # Django Auth Fields
    is_active = models.BooleanField(default=True)  # Can be disabled
    is_staff = models.BooleanField(default=False)  # Access to admin panel

    objects = CustomUserManager()

    USERNAME_FIELD = "email"  # Use email as the unique identifier
    REQUIRED_FIELDS = ["username"]

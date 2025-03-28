#!/bin/bash
django-admin startproject apis
cd apis
python manage.py startapp chat
# Edit chat/settings.py:
## Add chat as INSTALLED_APPS.
## Edit DATABASES.

# DB plan
python manage.py makemigrations chat
# DB apply
python manage.py migrate chat

# Run backend
python manage.py runserver 127.0.0.1:8890

# Create a user
curl --header "Content-Type: application/json" \
  --request POST \
  --data '{"email":"asd@asd.com","password":"xyz","username":"asd3"}' \
  http://192.168.10.2:8890/api/v2/user/signup/

# Run API without JWT
curl -H "Content-Type: application/json" \
  --request POST \
  --data '{"st_chat_group_name":"Chat Group1", "bl_personal":"True"}' \
  http://192.168.10.2:8890/api/v2/chatGroup/create/

# Run API with wrong JWT
curl -H "Content-Type: application/json" \
  -H "Authorization: Bearer ABC" \
  --request POST \
  --data '{"st_chat_group_name":"Chat Group1", "bl_personal":"True"}' \
  http://192.168.10.2:8890/api/v2/chatGroup/create/

# Login and get JWT
ACCESS=$(curl --header "Content-Type: application/json" --request POST --data '{"username":"asd3","password":"xyz"}' http://192.168.10.2:8890/api/v2/user/login/ | jq -r '.access')

# Create new chat groups
curl -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${ACCESS}" \
  --request POST \
  --data '{"st_chat_group_name":"Chat Group1", "bl_personal":"True"}' \
  http://192.168.10.2:8890/api/v2/chatGroup/create/

# Clear DB Users
python manage.py shell
# from chat.models import CustomUser
# CustomUser.objects.all().delete()  # Delete all users

# Run API after deleting the user
curl -H "Authorization: Bearer ${ACCESS}" \
  --request GET \
  http://192.168.10.2:8890/api/v2/test/

# Get all users
curl -H "Authorization: Bearer ${ACCESS}" \
  --request GET \
  http://192.168.10.2:8890/api/v2/user/listAllUsers/

# Get all chat groups
curl -H "Authorization: Bearer ${ACCESS}" \
  --request GET \
  http://192.168.10.2:8890/api/v2/chatGroup/myGroups/

# Join chat group
curl -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${ACCESS}" \
  --request POST \
  --data '{"st_chat_group_name":"Chat Group1"}' \
  http://192.168.10.2:8890/api/v2/chatGroup/join/


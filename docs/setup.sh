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
python manage.py runserver localhost:8000

# Create a user
curl --header "Content-Type: application/json" \
  --request POST \
  --data '{"email":"asd@asd.com","password":"xyz","username":"asd2"}' \
  http://localhost:8000/api/v1/register/

# Run API without JWT
curl -H "Content-Type: application/json" \
  --request POST \
  --data '{"st_chat_group_name":"Chat Group1", "bl_personal":"True"}' \
  http://localhost:8000/api/v1/createChatGroup/

# Run API with wrong JWT
curl -H "Content-Type: application/json" \
  -H "Authorization: Bearer ABC" \
  --request POST \
  --data '{"st_chat_group_name":"Chat Group1", "bl_personal":"True"}' \
  http://localhost:8000/api/v1/createChatGroup/

# Login and get JWT
ACCESS=$(curl --header "Content-Type: application/json" --request POST --data '{"username":"asd2","password":"xyz"}' http://localhost:8000/api/v1/login/ | jq -r '.access')

# Run API with JWT
curl -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${ACCESS}" \
  --request POST \
  --data '{"st_chat_group_name":"Chat Group2", "bl_personal":"True"}' \
  http://localhost:8000/api/v1/createChatGroup/

# Clear DB Users
python manage.py shell
# from chat.models import CustomUser
# CustomUser.objects.all().delete()  # Delete all users

# Run API after deleting the user
curl -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${ACCESS}" \
  --request GET \
  http://localhost:8000/api/v1/test/

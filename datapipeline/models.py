from django.db import models
from django.utils import timezone


class User(models.Model):
    username = models.CharField(max_length=100)
    email = models.EmailField(unique=True)
    university = models.CharField(max_length=100)
    assigned_id = models.CharField(max_length=100)
    joined_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.username

class Message(models.Model):
    session_id = models.CharField(max_length=100)
    student_id = models.CharField(max_length=100)
    sent_by = models.CharField(max_length = 20)
    created_at = models.DateTimeField(auto_now_add=True)
    content = models.TextField()
    gpt_used = models.CharField(max_length=100)

    def __str__(self):
        return f"{self.student_id} used {self.gpt_used}"

class FeedbackMessage(models.Model):
    session_id = models.CharField(max_length=100)
    student_id = models.CharField(max_length=100)
    sent_by = models.CharField(max_length = 20)
    created_at = models.DateTimeField(auto_now_add=True)
    content = models.TextField()
    gpt_used = models.CharField(max_length=100)
    gpt_id = models.IntegerField(null=True, blank=True)

    def __str__(self):
        return f"{self.student_id} used {self.gpt_used}"


class Course(models.Model):
    course_id = models.SlugField(max_length=50, unique=True)
    course_name = models.CharField(max_length=200)
    instructor_name = models.CharField(max_length=100)
    password = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.course_name} ({self.course_id})"


class FeedbackGPT(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=100)
    created_by = models.CharField(max_length=100, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    instructions = models.TextField()
    updated_at = models.DateTimeField(auto_now=True)
    course = models.ForeignKey(Course, on_delete=models.SET_NULL, null=True, blank=True, related_name='surveys')
    week_number = models.IntegerField(null=True, blank=True)
    survey_label = models.CharField(max_length=200, blank=True, default='')

    def __str__(self):
        return self.name


class CustomGPT(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=100)
    created_by = models.CharField(max_length=100, default='Sai')
    university = models.CharField(max_length=100, default='UCSC')
    gpt_type = models.CharField(max_length=50,default='')
    instructions = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


class FireData(models.Model):
    data = models.JSONField()


class Image(models.Model):
    image = models.ImageField(upload_to='images/')
    title = models.CharField(max_length=200, blank=True)
    description = models.TextField(blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.title or f"Image {self.id}"

    @property
    def image_url(self):
        if self.image:
            return self.image.url
        return None

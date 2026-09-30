from django.urls import path

from apps.jobs.api import (
    JobArtifactView,
    JobCancelView,
    JobCollectionView,
    JobDetailView,
    JobStatusView,
)

urlpatterns = [
    path("jobs", JobCollectionView.as_view(), name="internal-jobs"),
    path("jobs/status", JobStatusView.as_view(), name="internal-jobs-status"),
    path("jobs/<uuid:job_id>", JobDetailView.as_view(), name="internal-job-detail"),
    path("jobs/<uuid:job_id>/cancel", JobCancelView.as_view(), name="internal-job-cancel"),
    path("jobs/<uuid:job_id>/artifact", JobArtifactView.as_view(), name="internal-job-artifact"),
]

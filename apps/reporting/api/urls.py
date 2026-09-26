from django.urls import path

from apps.reporting.api.views import GeneralLedgerView, PeriodActivityView, TrialBalanceView

urlpatterns = [
    path("trial-balance", TrialBalanceView.as_view(), name="trial-balance"),
    path("period-activity", PeriodActivityView.as_view(), name="period-activity"),
    path("general-ledger", GeneralLedgerView.as_view(), name="general-ledger"),
]

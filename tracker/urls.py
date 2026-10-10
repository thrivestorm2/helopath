from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("aircraft/", views.aircraft_list, name="aircraft_list"),
    path("aircraft/<int:pk>/", views.aircraft_detail, name="aircraft_detail"),
    path("aircraft/<int:pk>/apply/", views.aircraft_apply, name="aircraft_apply"),
    path("sites/", views.site_list, name="site_list"),
    path("sites/<int:pk>/", views.site_detail, name="site_detail"),
    path("sites/<int:pk>/hangar/", views.set_weather_hangar, name="set_weather_hangar"),
    path("operators/<int:pk>/", views.operator_detail, name="operator_detail"),
    path("operators/<int:pk>/acquired/", views.operator_acquired, name="operator_acquired"),
    path("programs/<int:pk>/", views.program_detail, name="program_detail"),
    path("missions/", views.missions, name="missions"),
    path("routes/map/", views.route_map, name="route_map"),
    path("flows/", views.flows, name="flows"),
    path("flows/export.csv", views.flows_csv, name="flows_csv"),
    path("quality/", views.quality, name="quality"),
    path("ingest/", views.ingest_log, name="ingest_log"),
    path("review/", views.review_queue, name="review_queue"),
    path("review/<int:pk>/", views.review_action, name="review_action"),
    path("review/rules/", views.review_rules, name="review_rules"),
    path("clusters/", views.cluster_queue, name="cluster_queue"),
    path("clusters/<int:pk>/dismiss/", views.cluster_dismiss, name="cluster_dismiss"),
    path("rebuild/", views.queue_rebuild, name="queue_rebuild"),
    path("operations/", views.operations, name="operations"),
]

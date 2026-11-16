from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("myapp", "0004_alter_chargingsession_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="chargingsession",
            name="sepay_transaction_id",
            field=models.CharField(
                blank=True, max_length=100, null=True, unique=True
            ),
        ),
        migrations.AddField(
            model_name="chargingsession",
            name="mqtt_start_sent_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="chargingsession",
            name="status",
            field=models.CharField(
                choices=[
                    ("paid", "Đã thanh toán"),
                    ("waiting", "Đang chờ cổng trống"),
                    ("assigned", "Đã gán cổng"),
                    ("charging", "Đang sạc"),
                    ("done", "Hoàn tất"),
                ],
                max_length=20,
            ),
        ),
    ]

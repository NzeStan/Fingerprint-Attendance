# Example project

Shows `django-fingerprint-attendance` with:

- a custom employee model (`hr.Employee`, badge number used as device PIN)
- Celery (task backend and beat schedule), `config/celery.py`
- Channels realtime (`config/asgi.py`, WebSocket at `/ws/fingerprint-attendance/events/`)
- a custom attendance processor adding an `overtime` status (`hr/processing.py`)
- a hook and a signed webhook receiver (`hr/hooks.py`, `hr/views.py`)
- Nigerian public holidays from the `holidays` library, overridable in the admin

```bash
pip install -e "..[all]" daphne
cd example_project
python manage.py migrate
python manage.py createsuperuser
CELERY_EAGER=1 python manage.py runserver 0.0.0.0:8000     # tasks run inline
# or: celery -A config worker -B -l info   (with CELERY_EAGER=0 and Redis running)
```

Point a device's ADMS server at `http://<this-host>:8000`, approve it in `/admin/`, add
employees, enroll them (`/api/docs/` has the full API), and watch punches arrive.

Try it without hardware using the simulator:

```python
python manage.py shell
>>> from django.test import Client
>>> from fingerprint_attendance.testing import ADMSDeviceSimulator
>>> sim = ADMSDeviceSimulator(Client(SERVER_NAME="localhost"), serial="DEMO1")
>>> sim.handshake()
```

To receive the package's webhooks locally, create a webhook endpoint pointing to
`http://localhost:8000/hooks/attendance/`.

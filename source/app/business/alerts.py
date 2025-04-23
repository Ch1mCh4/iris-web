#  IRIS Source Code
#  Copyright (C) 2024 - DFIR-IRIS
#  contact@dfir-iris.org
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU Lesser General Public
#  License as published by the Free Software Foundation; either
#  version 3 of the License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
#  Lesser General Public License for more details.
#
#  You should have received a copy of the GNU Lesser General Public License
#  along with this program; if not, write to the Free Software Foundation,
#  Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.

import json
from datetime import datetime
from marshmallow.exceptions import ValidationError

from app import db
from app import socket_io
from app.iris_engine.access_control.iris_user import iris_current_user
from app.models.alerts import Alert
from app.datamgmt.alerts.alerts_db import cache_similar_alert
from app.datamgmt.manage.manage_access_control_db import user_has_client_access
from app.iris_engine.module_handler.module_handler import call_modules_hook
from app.iris_engine.utils.tracker import track_activity
from app.util import add_obj_history_entry
from app.business.errors import BusinessProcessingError
from app.schema.marshables import AlertSchema
from app.schema.marshables import CaseAssetsSchema
from app.schema.marshables import IocSchema


def _load(request_data, **kwargs):
    try:
        alert_schema = AlertSchema()
        return alert_schema.load(request_data, **kwargs)
    except ValidationError as e:
        raise BusinessProcessingError('Data error', data=e.messages)


def alerts_create(request_data) -> Alert:

    ioc_schema = IocSchema()
    asset_schema = CaseAssetsSchema()

    iocs_list = request_data.pop('alert_iocs', [])
    assets_list = request_data.pop('alert_assets', [])

    iocs = ioc_schema.load(iocs_list, many=True, partial=True)
    assets = asset_schema.load(assets_list, many=True, partial=True)

    alert = _load(request_data)

    if not user_has_client_access(iris_current_user.id, alert.alert_customer_id):
        raise BusinessProcessingError('User not entitled to create alerts for the client')
    alert.alert_creation_time = datetime.utcnow()

    alert.iocs = iocs
    alert.assets = assets

    db.session.add(alert)
    db.session.commit()

    add_obj_history_entry(alert, 'Alert created')

    cache_similar_alert(alert.alert_customer_id, assets=assets_list, iocs=iocs_list, alert_id=alert.alert_id,
                        creation_date=alert.alert_source_event_time)

    alert = call_modules_hook('on_postload_alert_create', data=alert)

    track_activity(f"created alert #{alert.alert_id} - {alert.alert_title}", ctx_less=True)

    socket_io.emit('new_alert', json.dumps({
        'alert_id': alert.alert_id
    }), namespace='/alerts')

    return alert


def alerts_update(request_data, alert) -> Alert:

    alert_schema = AlertSchema()
    do_resolution_hook = False
    do_status_hook = False

    try:
        activity_data = []
        for key, value in request_data.items():
            old_value = getattr(alert, key, None)

            if type(old_value) is int:
                old_value = str(old_value)

            if type(value) is int:
                value = str(value)

            if old_value != value:
                if key == "alert_resolution_status_id":
                    do_resolution_hook = True
                if key == 'alert_status_id':
                    do_status_hook = True

                if key not in ["alert_content", "alert_note"]:
                    activity_data.append(f"\"{key}\" from \"{old_value}\" to \"{value}\"")
                else:
                    activity_data.append(f"\"{key}\"")

        updated_alert = alert_schema.load(request_data, instance=alert, partial=True)
        if request_data.get('alert_owner_id') is None and updated_alert.alert_owner_id is None:
            updated_alert.alert_owner_id = iris_current_user.id

        if request_data.get('alert_owner_id') == "-1" or request_data.get('alert_owner_id') == -1:
            updated_alert.alert_owner_id = None

        db.session.commit()

        updated_alert = call_modules_hook('on_postload_alert_update', data=updated_alert)

        if do_resolution_hook:
            updated_alert = call_modules_hook('on_postload_alert_resolution_update', data=updated_alert)

        if do_status_hook:
            updated_alert = call_modules_hook('on_postload_alert_status_update', data=updated_alert)

        if activity_data:
            activity_data_as_string = ','.join(activity_data)
            track_activity(f'updated alert #{alert.alert_id}: {activity_data_as_string}', ctx_less=True)
            add_obj_history_entry(updated_alert, f'updated alert: {activity_data_as_string}')
        else:
            track_activity(f'updated alert #{alert.alert_id}', ctx_less=True)
            add_obj_history_entry(updated_alert, 'updated alert')

        db.session.commit()

        return alert
    except ValidationError as e:
        raise BusinessProcessingError('Data error', data=e.normalized_messages())

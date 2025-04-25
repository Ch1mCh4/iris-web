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
from app.datamgmt.alerts.alerts_db import delete_similar_alert_cache
from app.datamgmt.alerts.alerts_db import delete_related_alerts_cache
from app.datamgmt.alerts.alerts_db import get_alert_by_id
from app.datamgmt.manage.manage_access_control_db import user_has_client_access
from app.iris_engine.module_handler.module_handler import call_modules_hook
from app.iris_engine.utils.tracker import track_activity
from app.util import add_obj_history_entry
from app.business.errors import BusinessProcessingError
from app.schema.marshables import AlertSchema
from app.schema.marshables import CaseAssetsSchema
from app.schema.marshables import IocSchema
from app.blueprints.responses import response_success


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


def alerts_delete(alert_id):

    alert = get_alert_by_id(alert_id)

    try:

        if not user_has_client_access(iris_current_user.id, alert.alert_customer_id):
            raise BusinessProcessingError('User not entitled to delete alerts for the client')

        delete_similar_alert_cache(alert_id=alert_id)

        delete_related_alerts_cache([alert_id])

        db.session.delete(alert)
        db.session.commit()

        call_modules_hook('on_postload_alert_delete', data=alert_id)

        track_activity(f"delete alert #{alert_id}", ctx_less=True)

        return response_success(data={'alert_id': alert_id})

    except ValidationError as e:
        raise BusinessProcessingError('Data error', data=e.normalized_messages())

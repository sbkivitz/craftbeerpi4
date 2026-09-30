import logging

from aiohttp import web
from cbpi.api import *
from cbpi.api.dataclasses import Props, Sensor

auth = False


class SensorHttpEndpoints:

    def __init__(self, cbpi):
        self.cbpi = cbpi
        self.controller = cbpi.sensor
        self.cbpi.register(self, "/sensor")

    @request_mapping(path="/", auth_required=False)
    async def http_get_all(self, request):
        """

        ---
        description: Get all Sensors
        tags:
        - Sensor
        responses:
            "204":
                description: successful operation
        """
        return web.json_response(data=self.controller.get_state())

    @request_mapping(path="/", method="POST", auth_required=False)
    async def http_add(self, request):
        """
        ---
        description: Add one Sensor
        tags:
        - Sensor
        parameters:
        - in: body
          name: body
          description: Create a sensor
          required: true

          schema:
            type: object

            properties:
              name:
                type: string
              type:
                type: string
              props:
                type: object
            example:
              name: "Sensor"
              type: "CustomSensor"
              props: {}

        responses:
            "200":
                description: successful operation
        """
        data = await request.json()
        sensor = Sensor(
            name=data.get("name"),
            props=Props(data.get("props", {})),
            type=data.get("type"),
        )
        response_data = await self.controller.add(sensor)

        return web.json_response(data=response_data.to_dict())

    @request_mapping(path="/{id}", method="PUT", auth_required=False)
    async def http_update(self, request):
        """
        ---
        description: Update a sensor with given id
        tags:
        - Sensor
        parameters:
        - name: "id"
          in: "path"
          description: "Sensor ID"
          required: true
          type: "string"
        - in: body
          name: body
          description: Update a sensor with given id
          required: false
          schema:
            type: object
            properties:
              name:
                type: string
              type:
                type: string
              props:
                type: object
            example:
                name: "Raumtemperatur"
                type: "OneWire"
                props: 
                    Sensor: "28-3c01d60748af"
                    Interval: "10"
                    offset: "0"
        responses:
            "200":
                description: successful operation
        """
        id = request.match_info["id"]
        data = await request.json()
        sensor = Sensor(
            id=id,
            name=data.get("name"),
            props=Props(data.get("props", {})),
            type=data.get("type"),
        )
        return web.json_response(data=(await self.controller.update(sensor)).to_dict())

    @request_mapping(path="/{id}", method="DELETE", auth_required=False)
    async def http_delete_one(self, request):
        """
        ---
        description: Delete a sensor with given id
        tags:
        - Sensor
        parameters:
        - name: "id"
          in: "path"
          description: "Sensor ID"
          required: true
          type: "string"
        responses:
            "200":
                description: successful operation
        """
        id = request.match_info["id"]
        await self.controller.delete(id)
        return web.Response(status=200)

    @request_mapping(path="/{id}", method="GET", auth_required=False)
    async def get_value(self, request):
        """

        ---
        description: Get Sensor Value
        tags:
        - Sensor
        parameters:
        - name: "id"
          in: "path"
          description: "Sensor ID"
          type: "string"
          required: true
        """
        id = request.match_info["id"]
        sensor_value = self.controller.get_sensor_value(id)
        logging.info(sensor_value)
        return web.json_response(data=sensor_value)

    @request_mapping(path="/{id}/action", method="POST", auth_required=auth)
    async def http_action(self, request) -> web.Response:
        """

        ---
        description: Call Action for Sensor
        tags:
        - Sensor
        parameters:
        - name: "id"
          in: "path"
          description: "Sensor ID"
          required: true
          type: "integer"
          format: "int64"
        - in: body
          name: body
          description: Call an action for a Sensor
          required: false
          schema:
            type: object
            properties:
              action:
                type: string
              parameter:
                type: object
            example:
              action: "set"
              parameter:
                time: "5"
        responses:
            "200":
                description: successful operation
        """
        sensor_id = request.match_info["id"]
        data = await request.json()
        # Accept either key. The kettle endpoint has always taken `name`, this
        # one only ever took `action`, and nothing says which is which at the
        # call site - sending the wrong one produced a 200 and an action named
        # None, refused in the log where nobody was looking.
        action = data.get("action", data.get("name"))
        ok = await self.controller.call_action(
            sensor_id, action, data.get("parameter")
        )

        # Report failure to the caller, as the kettle endpoint does.
        #
        # This answered 200 whatever happened, so an action that was refused -
        # wrong name, no running instance - was acknowledged as applied. It
        # cost a real diagnosis: a vessel reset came back 200 and did nothing,
        # and the reason was only ever in the log.
        if ok is False:
            return web.json_response(
                {
                    "error": "action not run",
                    "detail": (
                        "'{}' did not run. Either the sensor is not running, "
                        "or that is not a declared action.".format(action)
                    ),
                },
                status=400,
            )

        return web.Response(status=200)

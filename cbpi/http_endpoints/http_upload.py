import logging

from aiohttp import web
from cbpi.api import *
from cbpi.api.dataclasses import Props, Step
from cbpi.controller.upload_controller import UploadController


class UploadHttpEndpoints:

    def __init__(self, cbpi):
        self.cbpi = cbpi
        self.controller: UploadController = cbpi.upload
        self.cbpi.register(self, "/upload")

    @request_mapping(path="/", method="POST", auth_required=False)
    async def FileUpload(self, request):
        """
        description: FileUpload 
        tags:
        - Upload
        parameters:
        - in: formData
          name: File
          type: file
          description: Recipe file to upload.

        responses:
            "200":
                description: successful operation
        """
        data = await request.post()
        logging.info("Received file upload request with data: {}".format(data))
        # 400 when it was not kept, so the Upload page - which reloads on
        # success - leaves the error on screen (DEFECTS M45).
        ok = await self.controller.FileUpload(data)
        return web.Response(status=200 if ok else 400)

    @request_mapping(path="/kbh", method="GET", auth_required=False)
    async def get_kbh_list(self, request):
        """

        ---
        description: Get Recipe list from Kleiner Brauhelfer
        tags:
        - Upload
        responses:
            "200":
                description: successful operation
        """

        kbh_list = await self.controller.get_kbh_recipes()
        return web.json_response(kbh_list)

    @request_mapping(path="/kbh", method="POST", auth_required=False)
    async def create_kbh_recipe(self, request):
        """

        ---
        description: Create Recipe from KBH database with selected ID
        tags:
        - Upload
        parameters:
        - name: "id"
          in: "body"
          description: "Recipe ID: {'id': ID}"
          required: true
          type: "string"
        responses:
            "200":
                description: successful operation
        """

        kbh_id = await request.json()
        ok, message = await self.controller.import_recipe("kbh", kbh_id.get("id"))
        return self._import_result(ok, message)

    @staticmethod
    def _import_result(ok, message):
        """200 and the recipe's name, or 400 and why nothing was imported.

        These answered 200 whatever happened, so a recipe that could not be
        read - which left the mash profile empty - was reported as created
        (DEFECTS M39).
        """
        if ok:
            return web.json_response({"name": message}, status=200)
        return web.json_response({"error": "recipe not imported", "detail": message},
                                 status=400)

    @request_mapping(path="/xml", method="GET", auth_required=False)
    async def get_xml_list(self, request):
        """

        ---
        description: Get recipe list from xml file
        tags:
        - Upload
        responses:
            "200":
                description: successful operation
        """

        xml_list = await self.controller.get_xml_recipes()

        return web.json_response(xml_list)

    @request_mapping(path="/xml", method="POST", auth_required=False)
    async def create_xml_recipe(self, request):
        """

        ---
        description: Create recipe from xml file with selected id
        tags:
        - Upload
        parameters:
        - name: "id"
          in: "body"
          description: "Recipe ID: {'id': ID}"
          required: true
          type: "string"

        responses:
            "200":
                description: successful operation
        """

        xml_id = await request.json()
        ok, message = await self.controller.import_recipe("xml", xml_id.get("id"))
        return self._import_result(ok, message)

    @request_mapping(path="/json", method="GET", auth_required=False)
    async def get_json_list(self, request):
        """

        ---
        description: Get recipe list from json file
        tags:
        - Upload
        responses:
            "200":
                description: successful operation
        """

        json_list = await self.controller.get_json_recipes()

        return web.json_response(json_list)

    @request_mapping(path="/json", method="POST", auth_required=False)
    async def create_json_recipe(self, request):
        """

        ---
        description: Create recipe from json file with selected id
        tags:
        - Upload
        parameters:
        - name: "id"
          in: "body"
          description: "Recipe ID: {'id': ID}"
          required: true
          type: "string"

        responses:
            "200":
                description: successful operation
        """

        json_id = await request.json()
        ok, message = await self.controller.import_recipe("json", json_id.get("id"))
        return self._import_result(ok, message)

    @request_mapping(path="/bf/{offset}/", method="POST", auth_required=False)
    async def get_bf_list(self, request):
        """

        ---
        description: Get recipe list from Brewfather App
        tags:
        - Upload
        parameters:
        - name: "offset"
          in: "path"
          description: "offset for recipe list"
          required: true
          type: "integer"
          format: "int64"
        responses:
            "200":
                description: successful operation
        """
        offset = request.match_info["offset"]
        bf_list = await self.controller.get_brewfather_recipes(offset)

        return web.json_response(bf_list)

    @request_mapping(path="/bfupdate/", method="GET", auth_required=False)
    async def get_bf_update(self, request):
        """

        ---
        description: Get recipe list update from Brewfather App
        tags:
        - Upload
        responses:
            "200":
                description: successful operation
        """
        # offset = request.match_info['offset']
        bf_list = await self.controller.get_brewfather_recipes()
        self.cbpi.ws.send(dict(topic="bfupdate", data=bf_list))
        return web.Response(status=200)

    @request_mapping(path="/bf", method="POST", auth_required=False)
    async def create_bf_recipe(self, request):
        """

        ---
        description: Create recipe from Brewfather Web App with selected id
        tags:
        - Upload
        parameters:
        - name: "id"
          in: "body"
          description: "Recipe ID: {'id': ID}"
          required: true
          type: "string"

        responses:
            "200":
                description: successful operation
        """

        bf_id = await request.json()
        ok, message = await self.controller.import_recipe("bf", bf_id.get("id"))
        return self._import_result(ok, message)

    @request_mapping(path="/getpath", auth_required=False)
    async def http_getpath(self, request):
        """

        ---
        description: get path for recipe creation
        tags:
        - Upload
        responses:
            "200":
                description: successful operation
        """
        return web.json_response(data=self.controller.get_creation_path())

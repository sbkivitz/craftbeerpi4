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
        # success - leaves the error on screen.
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
        description: Keep the recipe with this id from the uploaded Kleiner Brauhelfer database in the Recipe Book. The brew steps are left as they are
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
                description: "kept in the Recipe Book: {name, file, replaced, warnings: [{title, text}]}"
            "400":
                description: "not imported, nothing added to the Recipe Book: {error: recipe not imported, detail}"
        """

        kbh_id = await request.json()
        ok, result = await self.controller.import_recipe("kbh", kbh_id.get("id"))
        return self._import_result(ok, result)

    @staticmethod
    def _import_result(ok, result):
        """200 and {"name", "file", "replaced", "warnings"} - the recipe kept in the Recipe Book, the brew steps
        left as they are - or 400 {"error": "recipe not imported", "detail"}: why nothing was added to the book.

        Answering 200 whatever happened would report a recipe that could not
        be read as imported.
        """
        if ok:
            return web.json_response(result, status=200)
        return web.json_response({"error": "recipe not imported", "detail": result},
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
        description: Keep the recipe with this id from the uploaded BeerXML file in the Recipe Book, with a BeerXML file of its own (basic data source_xml, source_xml_recipe), and in its basic data BeerSmith's mash figures (mash_numbers), what goes into the mash and the sparge water (mash_additions, sparge_additions - [{name, type, use, amount, label, remind}], remind true for the water salts and acids the dough-in and the sparge name), its mash profile's pH (mash_ph - {value, from - mash_profile}) and its yeasts (yeasts). The brew steps are left as they are. Where the book has a recipe of the same name, the page says which way - "update" (UPDATE that book recipe, keeping its steps if the brewer changed it, and give the brew steps loaded from it its mash figures at once) or "new" (ADD AS A NEW RECIPE) - and with neither an earlier import of the name is replaced
        tags:
        - Upload
        parameters:
        - name: "id"
          in: "body"
          description: "Recipe ID: {'id': ID}, and optionally 'update': a book file's id, or 'new': true"
          required: true
          type: "string"

        responses:
            "200":
                description: "kept in the Recipe Book: {name, file, replaced, warnings: [{title, text}]}; for update also {updated: true, steps: kept or file, loaded}"
            "400":
                description: "not imported, nothing added to (or, for update, changed in) the Recipe Book: {error: recipe not imported, detail}"
        """

        xml_id = await request.json()
        how = UploadHttpEndpoints._how(xml_id)
        if isinstance(how, web.Response):
            return how
        ok, result = await self.controller.import_recipe("xml", xml_id.get("id"), **how)
        return self._import_result(ok, result)

    @classmethod
    def _how(cls, body):
        """{update or new} for import_recipe from an import's body: "update", a book file's id, or "new": true. A
        400 response for anything else, nothing imported."""
        how = {}
        update, new = body.get("update"), body.get("new")
        if update is not None:
            if not isinstance(update, str) or not update:
                return cls._import_result(False, "update is the id of a recipe in the Recipe Book, as GET /recipe/ "
                                                 "lists it. Nothing was changed in the Recipe Book.")
            how["update"] = update
        if new is not None:
            if not isinstance(new, bool):
                return cls._import_result(False, "new is true or false. Nothing was added to the Recipe Book.")
            if new:
                how["new"] = True
        return how

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
        description: Keep the recipe with this id from the uploaded MMuM-JSON file in the Recipe Book. The brew steps are left as they are
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
                description: "kept in the Recipe Book: {name, file, replaced, warnings: [{title, text}]}"
            "400":
                description: "not imported, nothing added to the Recipe Book: {error: recipe not imported, detail}"
        """

        json_id = await request.json()
        ok, result = await self.controller.import_recipe("json", json_id.get("id"))
        return self._import_result(ok, result)

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
        description: Keep the Brewfather recipe with this id in the Recipe Book. The brew steps are left as they are
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
                description: "kept in the Recipe Book: {name, file, replaced, warnings: [{title, text}]}"
            "400":
                description: "not imported, nothing added to the Recipe Book: {error: recipe not imported, detail}"
        """

        bf_id = await request.json()
        ok, result = await self.controller.import_recipe("bf", bf_id.get("id"))
        return self._import_result(ok, result)

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

#!/usr/local/bin/python3.7
# -*- coding: utf-8 -*-

""" NuvlaBox Agent API

List of functions to support the NuvlaBox Agent API instantiated by app.py
"""

import json
import logging
import os
import glob
import socket
import nuvla.api

from agent.common import NuvlaBoxCommon

nuvla_resource = "nuvlabox-peripheral"
NB = NuvlaBoxCommon.NuvlaBoxCommon()


def local_peripheral_exists(filepath):
    """ Check if a local file copy of the Nuvla peripheral resource already exists

    :param filepath: path of the file in the .peripherals folder
    :returns boolean
    """

    if os.path.exists(filepath):
        return True

    return False


def local_peripheral_save(filepath, content):
    """ Create a local file copy of the Nuvla peripheral resource

    :param filepath: path of the file to be written in the .peripherals folder
    :param content: content of the file in JSON format
    """

    with open(filepath, 'w') as f:
        f.write(json.dumps(content))


def local_peripheral_get_identifier(filepath):
    """ Reads the content of a local copy of the NB peripheral, and gets the Nuvla ID

    :param filepath: path of the peripheral file in .peripherals, to be read
    :returns ID
    """

    try:
        with open(filepath) as f:
            peripheral_nuvla_id = json.loads(f.read())["id"]
    except:
        # if something happens, just return None
        return None

    return peripheral_nuvla_id


def post(payload):
    """ Creates a new nuvlabox-peripheral resource in Nuvla

    :param payload: base JSON payload for the nuvlabox-peripheral resource
    :returns request message and status
    """

    if not payload or not isinstance(payload, dict):
        # Invalid payload
        logging.error("Payload {} malformed. It must be a JSON payload".format(payload))
        return {"error": "Payload {} malformed. It must be a JSON payload".format(payload)}, 400

    try:
        peripheral_identifier = payload['identifier']
    except KeyError as e:
        logging.error("Payload {} is incomplete. Missing 'identifier'. {}".format(payload, e))
        return {"error": "Payload {} is incomplete. Missing 'identifier'. {}".format(payload, e)}, 400

    peripheral_filepath = "{}/{}".format(NB.peripherals_dir, peripheral_identifier)

    # Check if peripheral already exists locally before pushing to Nuvla
    if local_peripheral_exists(peripheral_filepath):
        logging.error("Peripheral %s file already registered. Please delete it first" % peripheral_identifier)
        return {"error": "Peripheral %s file already registered. Please delete it first" % peripheral_identifier}, 400

    # complete the payload with the NB specific attributes, in case they are missing
    if 'parent' not in payload:
        payload['parent'] = NB.nuvlabox_id

    if 'version' not in payload:
        if os.path.exists("{}/{}".format(NB.data_volume, NB.context)):
            version = json.loads(open("{}/{}".format(NB.data_volume, NB.context)).read())['version']
        else:
            try:
                tag = NB.docker_client.api.inspect_container(socket.gethostname())['Config']['Labels']['git.branch']
                version = int(tag.split('.')[0])
            except (KeyError, ValueError, IndexError):
                version = 1

        payload['version'] = version

    # Try to POST the resource
    try:
        logging.info("Posting peripheral {}".format(payload))
        new_peripheral = NB.api().add(nuvla_resource, payload)
    except nuvla.api.api.NuvlaError as e:
        logging.exception("Failed to POST peripheral")
        return e.response.json(), e.response.status_code
    except Exception as e:
        logging.exception("Unable to POST peripheral to Nuvla")
        return {"error": "Unable to complete POST request: {}".format(e)}, 500

    payload['id'] = new_peripheral.data['resource-id']

    try:
        logging.info("Saving peripheral %s locally" % payload['id'])
        local_peripheral_save(peripheral_filepath, payload)
    except Exception as e:
        logging.exception("Unable to save peripheral. Reverting request...")
        delete(peripheral_identifier, peripheral_nuvla_id=payload['id'])
        return {"error": "Unable to fulfill request: %s" % e}, 500

    return new_peripheral.data, new_peripheral.data['status']


def delete(peripheral_identifier, peripheral_nuvla_id=None):
    """ Deletes a peripheral from the local and Nuvla database

    :param peripheral_identifier: unique local identifier for the peripheral
    :param peripheral_nuvla_id: (optional) Nuvla ID for the peripheral resource. If present, will not infer it from
    the local file copy of the peripheral resource
    :returns request message and status
    """

    peripheral_filepath = "{}/{}".format(NB.peripherals_dir, peripheral_identifier)

    if not local_peripheral_exists(peripheral_filepath):
        # local peripheral file does not exist, let's check in Nuvla
        logging.info("{} does not exist locally. Checking in Nuvla...".format(peripheral_filepath))
        if peripheral_nuvla_id:
            try:
                delete_peripheral = NB.api().delete(peripheral_nuvla_id)
                logging.info("Deleted {} from Nuvla".format(peripheral_nuvla_id))
                return delete_peripheral.data, delete_peripheral.data['status']
            except nuvla.api.api.NuvlaError as e:
                logging.warning("While deleting {} from Nuvla: {}".format(peripheral_nuvla_id, e.response.json()))
                return e.response.json(), e.response.status_code
        else:
            logging.warning("{} not found and Nuvla resource ID not provided".format(peripheral_filepath))
            return {"error": "Peripheral not found"}, 404
    else:
        # file exists, but before deleting it, check if we need to infer the Nuvla ID from it
        if not peripheral_nuvla_id:
            peripheral_nuvla_id = local_peripheral_get_identifier(peripheral_filepath)

        if peripheral_nuvla_id:
            try:
                delete_peripheral = NB.api().delete(peripheral_nuvla_id)
                logging.info("Deleted {} from Nuvla".format(peripheral_nuvla_id))

                os.remove(peripheral_filepath)
                logging.info("Deleted {} from the NuvlaBox".format(peripheral_filepath))

                return delete_peripheral.data, delete_peripheral.data['status']
            except nuvla.api.api.NuvlaError as e:
                if e.response.status_code != 404:
                    logging.warning("While deleting {} from Nuvla: {}".format(peripheral_nuvla_id, e.response.json()))
                    # Maybe something went wrong and we should try later, so keep the local peripheral copy alive
                    return e.response.json(), e.response.status_code
            except Exception as e:
                # for any other deletion problem, report
                logging.exception("While deleting {} from Nuvla".format(peripheral_nuvla_id))
                return {"error": "Error occurred while deleting {}: {}".format(peripheral_identifier, e)}, 500

        # Even if the peripheral does not exist in Nuvla anymore, let's delete it locally
        os.remove(peripheral_filepath)
        logging.info("Deleted {} from the NuvlaBox".format(peripheral_filepath))
        return {"message": "Deleted %s" % peripheral_identifier}, 200


def find(parameter, value, identifier_pattern):
    """ Finds all locally registered peripherals that match parameter=value

    :param parameter: name of the parameter to search for
    :param value: value of that parameter
    :param identifier_pattern: regex expression to limit the search query to peripherals matching the identifier pattern
    :returns list of peripheral matching the search query
    """

    matched_peripherals = []

    search_dir = "{}/{}".format(NB.peripherals_dir, identifier_pattern) if identifier_pattern \
        else NB.peripherals_dir + "/*"

    for filename in glob.glob(search_dir):
        if parameter and value:
            with open(filename) as f:
                try:
                    content = json.loads(f.read())
                except:
                    continue

                if parameter in content and content[parameter] == value:
                    matched_peripherals.append(os.path.basename(filename))
        else:
            matched_peripherals.append(os.path.basename(filename))

    return matched_peripherals, 200

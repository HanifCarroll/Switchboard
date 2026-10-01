"""Point synthetic customer records at the deployed, controlled receiver."""

from copy import deepcopy
from urllib.parse import urlsplit

from switchboard.configuration import runtime_settings


def bind_receiver_destinations(payload: dict) -> dict:
    base = runtime_settings().receiver_url
    if base is None:
        return payload

    # 1. Build a mapping without adding any registered destinations.
    payload = deepcopy(payload)
    records = payload["records"]
    endpoints = {
        item["url"]
        for customer in records["customers"]
        for item in customer["registered_destinations"]
    }
    endpoints.update(item["endpoint"] for item in records["integrations"])
    endpoints.update(item["requested_endpoint"] for item in records["tickets"])
    replacements = {}
    for endpoint in endpoints:
        parsed = urlsplit(endpoint)
        if parsed.hostname and parsed.hostname.endswith(".example"):
            replacements[endpoint] = (
                f"{str(base).rstrip('/')}/destinations/{parsed.hostname}{parsed.path}"
            )

    # 2. Keep registry, configuration, request, and quoted URLs consistent.
    for customer in records["customers"]:
        for destination in customer["registered_destinations"]:
            destination["url"] = replacements.get(
                destination["url"], destination["url"]
            )
    for integration in records["integrations"]:
        integration["endpoint"] = replacements.get(
            integration["endpoint"], integration["endpoint"]
        )
    for ticket in records["tickets"]:
        ticket["requested_endpoint"] = replacements.get(
            ticket["requested_endpoint"], ticket["requested_endpoint"]
        )
        for previous, current in replacements.items():
            ticket["body"] = ticket["body"].replace(previous, current)
    for previous, current in replacements.items():
        payload["inputs"]["request"] = payload["inputs"]["request"].replace(
            previous, current
        )
    return payload

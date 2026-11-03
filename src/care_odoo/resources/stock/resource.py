"""Explicit monthly stock writes through the existing Care/Odoo connector."""

from care_odoo.connector.connector import OdooConnector


class OdooStockResource:
    @staticmethod
    def call(action, period, **payload):
        return OdooConnector.call_api("api/stock/period", {**period, "action": action, **payload})

    @classmethod
    def prepare(cls, period, items, batches, locations, opening_at):
        return cls.call("prepare", period, items=items, batches=batches, locations=locations, opening_at=opening_at)

    @classmethod
    def post_events(cls, period, events):
        return cls.call("events", period, events=events)

    @classmethod
    def report(cls, period, item_uuids, as_of):
        return cls.call("report", period, item_uuids=item_uuids, as_of=as_of)

    @classmethod
    def bill_sources(cls, period, line_uuids, order_uuids):
        return cls.call("bill_sources", period, care_line_uuids=line_uuids, care_order_uuids=order_uuids)

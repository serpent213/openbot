"""Versioned plugin documents. Settings guide tool use; they are not a shell sandbox."""

from __future__ import annotations

import os

from pydantic import ValidationError
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from openbot.db.models import PluginSetting, utcnow
from openbot.runtime.app_settings import MASK
from openbot.runtime.secrets import SecretBox, resolve_secret_key


class SettingsConflict(ValueError):
    pass


class SettingsStore:
    def __init__(self, services, plugin):
        self.services, self.plugin = services, plugin
        self.box = services.secrets or SecretBox(lambda: resolve_secret_key(services.settings))

    def model(self, scope):
        spec = self.plugin.settings
        model = getattr(spec, "global_" if scope == "global" else scope, None)
        if model is None or scope not in ("global", "agent"):
            raise ValueError("plugin does not declare this settings scope")
        return model

    def pins(self, scope):
        if scope != "global":
            return {}
        values = dict(self.services.settings.plugin_config.get(self.plugin.id, {}))
        for name, field in self.model(scope).model_fields.items():
            env = (field.json_schema_extra or {}).get("env")
            if env and env in os.environ:
                values[name] = os.environ[env]
        return values

    def secret_fields(self, scope):
        return {
            name
            for name, prop in self.model(scope).model_json_schema()["properties"].items()
            if prop.get("writeOnly") or any(p.get("writeOnly") for p in prop.get("anyOf", []))
        }

    def decode(self, scope, value):
        value = dict(value)
        for name in self.secret_fields(scope):
            if name in value and value[name] is not None:
                value[name] = self.box.decrypt(value[name])
                if value[name] is None:
                    raise ValueError("stored secret cannot be decrypted")
        return value

    def encode(self, scope, instance):
        value = instance.model_dump(mode="json")
        for name in self.secret_fields(scope):
            secret = getattr(instance, name)
            value[name] = (
                self.box.encrypt(secret.get_secret_value()) if secret is not None else None
            )
        return value

    def validate(self, scope, value):
        try:
            return self.model(scope).model_validate(value, extra="forbid")
        except ValidationError as exc:
            # Pydantic's normal error text includes input values, potentially secrets.
            fields = ", ".join(
                ".".join(map(str, e["loc"])) for e in exc.errors(include_input=False)
            )
            raise ValueError("invalid plugin settings: " + fields) from None

    async def read(self, scope, subject=""):
        async with self.services.session_factory() as session:
            row = await session.get(PluginSetting, (self.plugin.id, scope, subject))
        if row is None:
            return {}, 0
        value = self.decode(scope, row.value)
        spec = self.plugin.settings
        if row.schema_version != spec.version:
            if row.schema_version > spec.version or spec.migrate is None:
                raise ValueError("stored settings require a schema migration")
            value = spec.migrate(scope, row.schema_version, value)
        return value, row.revision

    async def get(self, scope, subject=""):
        value, _ = await self.read(scope, subject)
        return self.validate(scope, {**value, **self.pins(scope)})

    async def global_settings(self):
        return await self.get("global")

    async def for_agent(self, actor_id):
        return await self.get("agent", actor_id)

    async def describe(self, scope, subject=""):
        # Even invalid documents remain editable, with their revision intact. Never return
        # unvalidated stored values or secrets, and never silently enable default capabilities.
        async with self.services.session_factory() as session:
            row = await session.get(PluginSetting, (self.plugin.id, scope, subject))
        result = {
            "schema": self.model(scope).model_json_schema(),
            "revision": row.revision if row else 0,
            "schema_version": self.plugin.settings.version,
            "locked": sorted(self.pins(scope)),
            "value": None,
            "error": None,
        }
        try:
            instance = await self.get(scope, subject)
            value = instance.model_dump(mode="json")
            for name in self.secret_fields(scope):
                value[name] = MASK if getattr(instance, name) is not None else None
            result["value"] = value
        except (ValueError, TypeError):
            result["error"] = (
                "Stored or deployment settings are invalid; affected tools are disabled. Replace the document to repair it."
            )
        return result

    async def write(self, scope, subject, values, expected_revision, *, replace=False):
        self.model(scope)
        pins = self.pins(scope)
        if set(values) & pins.keys():
            raise ValueError(
                "settings locked by deployment: " + ", ".join(sorted(set(values) & pins.keys()))
            )
        async with self.services.session_factory() as session:
            key = (self.plugin.id, scope, subject)
            row = await session.get(PluginSetting, key)
            if (row.revision if row else 0) != expected_revision:
                raise SettingsConflict("settings changed; reload before saving")
            # PUT can repair even an unreadable old document. PATCH requires a valid schema version.
            old, _ = await self.read(scope, subject) if not replace else ({}, 0)
            values = dict(values)
            for name in self.secret_fields(scope):
                if values.get(name) == MASK:
                    if row is None:
                        raise ValueError("cannot preserve a secret that has not been set")
                    values[name] = self.decode(scope, row.value).get(name)
            merged = {**old, **values}
            instance = self.validate(scope, {**merged, **pins})
            encoded = self.encode(scope, instance)
            for name in pins:
                if row is not None and name in row.value:
                    encoded[name] = row.value[name]
                else:
                    encoded.pop(name, None)
            # Keep stored overrides underneath deployment pins; never persist the pins.
            data = {
                "value": encoded,
                "schema_version": self.plugin.settings.version,
                "revision": expected_revision + 1,
                "updated_at": utcnow(),
            }
            try:
                if row is None:
                    session.add(
                        PluginSetting(
                            plugin_id=self.plugin.id,
                            scope=scope,
                            subject=subject,
                            actor_id=subject if scope == "agent" else None,
                            **data,
                        )
                    )
                else:
                    result = await session.execute(
                        update(PluginSetting)
                        .where(
                            PluginSetting.plugin_id == self.plugin.id,
                            PluginSetting.scope == scope,
                            PluginSetting.subject == subject,
                            PluginSetting.revision == expected_revision,
                        )
                        .values(**data)
                    )
                    if result.rowcount != 1:
                        raise SettingsConflict("settings changed; reload before saving")
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raise SettingsConflict("settings changed; reload before saving") from None
        if self.services.bus:
            await self.services.bus.publish(
                self.plugin.id + ".settings.updated", None, {"scope": scope, "subject": subject}
            )
        return await self.describe(scope, subject)

    async def initialize_agent(self, actor_id, values):
        """Import an existing plugin policy once, preserving subsequent operator edits."""
        async with self.services.session_factory() as session:
            row = await session.get(PluginSetting, (self.plugin.id, "agent", actor_id))
        if row is None:
            try:
                await self.write("agent", actor_id, values, 0)
            except SettingsConflict:
                pass

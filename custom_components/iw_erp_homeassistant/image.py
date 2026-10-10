"""Image entities showing the ERP room displays (door signs).

One image entity per display the :class:`~.display.ErpDisplaySender` handles:
the displays assigned to an OpenDisplay panel (option "Room displays") and,
with the option "Room display images", every other ERP room display. The
sender fetches the frames with the ETag; the entities only show the last one.
"""
from __future__ import annotations

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .display import ErpDisplaySender


async def async_setup_entry(
    hass: HomeAssistant,
    entry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up an image entity per handled room display."""
    sender: ErpDisplaySender | None = entry.runtime_data.display_sender
    if sender is None:
        return
    names = entry.runtime_data.display_names
    async_add_entities(
        ErpDisplayImage(hass, sender, entry.entry_id, display_id, names.get(display_id))
        for display_id in sender.display_ids
    )


class ErpDisplayImage(ImageEntity):
    """The current frame of an ERP room display."""

    _attr_content_type = "image/png"

    def __init__(
        self,
        hass: HomeAssistant,
        sender: ErpDisplaySender,
        entry_id: str,
        display_id: str,
        name: str | None,
    ) -> None:
        super().__init__(hass)
        self._sender = sender
        self._display_id = display_id
        self._attr_unique_id = f"{entry_id}-display-image-{display_id}"
        self._attr_name = name or f"Room display {display_id}"
        self._attr_extra_state_attributes = {"erp_display_id": display_id}
        _image, self._attr_image_last_updated = sender.frame(display_id)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._sender.async_add_listener(self._display_id, self._frame_changed))

    @callback
    def _frame_changed(self) -> None:
        _image, self._attr_image_last_updated = self._sender.frame(self._display_id)
        self.async_write_ha_state()

    async def async_image(self) -> bytes | None:
        image, _updated = self._sender.frame(self._display_id)
        return image

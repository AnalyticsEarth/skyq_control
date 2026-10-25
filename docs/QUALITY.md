# Integration quality scale: self-assessment

Home Assistant's [integration quality scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/) applies formally only to core integrations. This assessment of `skyq_control` v0.6.0 against its rules uses these marks:

- ✅ done
- ➖ exempt (the reason is given)
- ⚠️ partly done

## Bronze

| Rule | | How |
|---|---|---|
| action-setup | ✅ | Actions are registered in `async_setup` (`services.py`) and target media player entities. |
| appropriate-polling | ✅ | Polls 5 s while on and 30 s in standby. Slow data is polled hourly, recordings every 15 min. Everything else is pushed. |
| brands | ✅ | `custom_components/skyq_control/brand/` holds an original icon, not Sky's logo. |
| common-modules | ✅ | `coordinator.py`, `entity.py`. |
| config-flow | ✅ | Discovery list, manual IP entry, SSDP. Selectors and `data_description` are used. |
| config-flow-test-coverage | ✅ | `tests/test_config_flow.py`, about 99 % line coverage. |
| dependency-transparency | ✅ | `aioskyq` is MIT, has its own tests and CI, and is bundled because HACS can't install a private PyPI package. |
| docs-actions | ✅ | README "Actions for automations", plus `services.yaml` descriptions. |
| docs-high-level-description | ✅ | README intro. |
| docs-installation-instructions | ✅ | README "Install with HACS". |
| docs-removal-instructions | ✅ | README "Removing the integration". |
| entity-event-setup | ✅ | Push subscriptions live in the coordinator. Event entities subscribe in `async_added_to_hass` and unsubscribe through `async_on_remove`. |
| entity-unique-id | ✅ | `{serial}_{key}`. |
| has-entity-name | ✅ | Every entity uses translation keys. |
| runtime-data | ✅ | `ConfigEntry[SkyQCoordinator]`. |
| test-before-configure | ✅ | The config flow reads device info before creating an entry. |
| test-before-setup | ✅ | The first refresh raises `ConfigEntryNotReady`, and a different box at the address raises `ConfigEntryError`. |
| unique-config-entry | ✅ | The unique ID is the serial number, with the MAC address as a fallback. |

## Silver

| Rule | | How |
|---|---|---|
| action-exceptions | ✅ | `ServiceValidationError` for bad input, `HomeAssistantError` for box failures. Both are translated. |
| config-entry-unloading | ✅ | Platforms unload. The coordinator stops its websockets and closes the box. |
| docs-configuration-parameters | ✅ | Option descriptions in the UI: key delay and poll interval. |
| docs-installation-parameters | ✅ | IP address, from discovery or entered by hand. |
| entity-unavailable | ✅ | Entities go unavailable when the box can't be reached. Status entities stay unavailable until the first push arrives. |
| integration-owner | ✅ | `@AnalyticsEarth`. |
| log-when-unavailable | ✅ | The coordinator logs once when a box goes away and once when it's back. |
| parallel-updates | ✅ | Set on every platform: 1 for commands, 0 for read-only platforms. |
| reauthentication-flow | ➖ | The box has no authentication. |
| test-coverage | ✅ | About 97 % for the integration (`pytest --cov`). |

## Gold

| Rule | | How |
|---|---|---|
| devices | ✅ | One device per box. Minis are linked to the gateway with `via_device_id`. |
| diagnostics | ✅ | Identifiers are redacted. |
| discovery | ✅ | SSDP (`GatewaySkyControl:2`, `MRSkyControl:2`). |
| discovery-update-info | ✅ | Rediscovery updates a known box's IP address. |
| docs-data-update | ✅ | README "Under the hood". |
| docs-examples | ✅ | README "Automations", plus `docs/dashboard.yaml`. |
| docs-known-limitations | ✅ | README "Known limitations". |
| docs-supported-devices | ✅ | README "Supported devices". |
| docs-supported-functions | ✅ | README entity table and actions. |
| docs-troubleshooting | ✅ | README "Troubleshooting". |
| docs-use-cases | ✅ | README "Automations". |
| dynamic-devices | ➖ | One config entry per box, and boxes don't add sub-devices. |
| entity-category | ✅ | Box details are `diagnostic`. |
| entity-device-class | ✅ | Timestamp, date, data size, enum, power, connectivity, problem and update. |
| entity-disabled-by-default | ✅ | Household ID and viewing-card number. |
| entity-translations | ✅ | |
| exception-translations | ✅ | |
| icon-translations | ✅ | `icons.json`, including state icons. |
| reconfiguration-flow | ✅ | Changes a box's IP address and checks that it's the same box. |
| repair-issues | ➖ | Nothing the user needs to repair. Every failure is either temporary or reported by an action. |
| stale-devices | ✅ | A Mini's recorder entities are removed once its gateway owns the recordings. There is one device per entry, so deleting the entry removes the device. |

## Platinum

| Rule | | How |
|---|---|---|
| async-dependency | ✅ | `aioskyq` uses asyncio and aiohttp. |
| inject-websession | ✅ | HA's shared aiohttp session is passed to `SkyQBox` and the guide client. |
| strict-typing | ⚠️ | The integration passes `mypy --strict` in CI, and `aioskyq` is strict too. `implicit_reexport` is allowed because HA's component modules re-export their public names. |

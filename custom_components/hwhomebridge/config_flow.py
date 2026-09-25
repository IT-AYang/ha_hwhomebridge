"""Config flow for HuaweiHome Bridge

配置流程：
1. 用户在HA设置中点击"添加集成" → 搜索"hwhomebridge"
2. 点击进入配置页面 → 显示提示页面（无PIN码）
3. 用户勾选确认后点击"提交" → 完成集成配置，启动网关
4. 用户再次点击"配置"按钮 → 选择功能：PIN绑定 或 设备管理
5a. PIN绑定 → 显示PIN码 → 在APP上绑定
5b. 设备管理 → 显示已接入设备列表 → 勾选/取消勾选设备
"""

import asyncio
import logging
import time
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .const import DOMAIN, SKIP_PLATFORMS
from .pin_manager import PINManager

_LOGGER = logging.getLogger(__name__)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for HuaweiHome Bridge.
    
    配置流程处理器：
    - 首次配置：显示提示页面，完成后启动网关
    - 后续配置：通过OptionsFlow显示PIN码或设备管理
    """
    
    VERSION = 1
    
    async def async_step_user(self, user_input=None):
        """第一步：显示欢迎页面
        
        用户首次添加集成时显示功能说明和使用步骤，
        用户点击"提交"后完成配置，网关自动启动。
        """
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        
        # 用户点击"提交"，创建配置条目
        if user_input is not None:
            _LOGGER.info("Config flow completed, gateway will start")
            return self.async_create_entry(
                title="Huawei HA Gateway",
                data={}
            )
        
        # 显示欢迎页面（无输入框）
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({}),  # 空表单
            last_step=True,
        )
    
    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """获取选项配置流程"""
        return OptionsFlowHandler()


class OptionsFlowHandler(config_entries.OptionsFlow):
    """选项配置流程处理器
    
    已配置后，点击"配置"按钮进入此流程。
    提供两个功能：
    - PIN绑定：显示PIN码用于在华为智慧生活APP上绑定
    - 设备管理：查看和管理已接入的设备（启用/禁用）
    """
    
    __DEFAULT_TIME_OUT = 300  # 5分钟
    
    def __init__(self):
        self._pin_task: asyncio.Task | None = None
        self._abort_reason = ""
    
    async def async_step_init(self, user_input=None):
        """选项配置入口：选择功能
        
        显示一个选择菜单，让用户选择是进行PIN绑定还是设备管理。
        """
        if user_input is not None:
            action = user_input.get("action")
            if action == "pin_binding":
                return await self.async_step_pin_binding()
            elif action == "device_management":
                return await self.async_step_select_device()
        
        # 显示选择菜单
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required("action", default="pin_binding"): vol.In({
                    "pin_binding": "PIN绑定",
                    "device_management": "设备管理",
                }),
            }),
        )
    
    async def async_step_pin_binding(self, user_input=None):
        """PIN绑定入口：生成PIN码并显示"""
        pin_code = PINManager.generate_pin()
        _LOGGER.info(f"Generated PIN for binding: {pin_code}")
        
        if self._pin_task is None:
            self._pin_task = self.hass.async_create_task(
                self._wait_for_binding(pin_code)
            )
        
        return await self.async_step_show_pin()
    
    async def async_step_show_pin(self, user_input=None):
        """显示PIN码页面（无提交按钮）"""
        # 检查后台任务状态
        if self._pin_task.done():
            # 任务完成，检查是否有异常
            if err := self._pin_task.exception():
                if isinstance(err, TimeoutError):
                    _LOGGER.warning("PIN expired after timeout")
                    self._abort_reason = "pin_expired"
                else:
                    _LOGGER.error(f"Unexpected error: {err}")
                    self._abort_reason = "internal_error"
            else:
                # 绑定成功
                _LOGGER.info("Binding completed successfully")
                self._abort_reason = ""
            
            # 进入下一步（成功或失败）
            return self.async_show_progress_done(next_step_id="finish")
        
        # 显示PIN码（使用progress，无提交按钮）
        time_out = self.__DEFAULT_TIME_OUT // 60  # 转换为分钟
        return self.async_show_progress(
            step_id="show_pin",
            progress_action="wait_for_binding",
            description_placeholders={
                "pin_code": str(PINManager.get_current_pin() or ""),
                "time_out": time_out,
            },
            progress_task=self._pin_task,
        )
    
    async def async_step_finish(self, user_input=None):
        """完成配置"""
        if self._abort_reason:
            # 超时或错误，中止流程
            return self.async_abort(reason=self._abort_reason)
        
        # 绑定成功，创建配置条目
        return self.async_create_entry(title="", data={})
    
    async def async_step_select_device(self, user_input=None):
        """设备管理：显示已接入设备列表，支持启用/禁用
        
        显示所有已匹配并注册到 HiLink 的设备，用户可以通过
        勾选/取消勾选来添加/移除设备的桥接。
        
        勾选 = 设备接入（创建设备条目，上报在线）
        取消勾选 = 设备移除（删除设备条目，上报离线）
        """
        from .hwbridge import service_router
        
        if service_router is None:
            return self.async_abort(reason="integration_not_init")
        
        all_vds = service_router.sn_manager.get_all_devices()
        
        if not all_vds:
            # 没有已接入的设备
            return self.async_show_form(
                step_id="select_device",
                data_schema=vol.Schema({}),
                description_placeholders={"device_count": "0"},
            )
        
        # 构建设备列表 {sn: "设备名 (PID)"}
        dev_reg = dr.async_get(self.hass)
        options_dict = {}
        default_selected = []
        
        # 获取 config entry_id 用于查找桥接设备条目
        entries = self.hass.config_entries.async_entries(DOMAIN)
        entry_id = entries[0].entry_id if entries else None
        
        for vd in all_vds:
            # 优先从原始 HA 设备获取名称
            name = vd.product.name or vd.product.pid
            orig_device = dev_reg.devices.get(vd.ha_device_id)
            if orig_device:
                name = orig_device.name_by_user or orig_device.name or name
            
            # 查找桥接设备条目（用于名称回退和默认选中判断）
            bridge_dev = None
            if entry_id:
                bridge_dev = dev_reg.async_get_device(
                    identifiers={(DOMAIN, entry_id, vd.sn)}
                )
            
            # 如果原始设备名称为空，尝试从桥接设备条目获取名称
            if not name and bridge_dev and bridge_dev.name:
                name = bridge_dev.name
            
            label = f"{name} ({vd.product.pid})"
            options_dict[vd.sn] = label
            
            # 默认选中：设备注册表中有对应条目的设备
            if bridge_dev is not None:
                default_selected.append(vd.sn)
        
        if user_input is not None:
            # 用户提交了选择
            selected_sns = set(user_input.get("devices", []))
            
            # 取消勾选的设备：从 HiLink 注销（status=2 删除） + 移除 HA 设备注册表条目
            # 勾选的设备：注册到 HiLink（status=3 新设备 + status=1 上线） + 创建 HA 设备注册表条目
            from .hwbridge import (
                _create_bridge_device_entry_async,
                _remove_bridge_device_entry_async,
            )

            for vd in all_vds:
                orig_device = dev_reg.devices.get(vd.ha_device_id)
                name = vd.product.name or vd.product.pid
                model = ""
                manufacturer = ""
                if orig_device:
                    name = orig_device.name_by_user or orig_device.name or name
                    model = orig_device.model or ""
                    manufacturer = orig_device.manufacturer or ""

                if vd.sn not in selected_sns:
                    # 取消勾选 → 注销设备（status=2）+ 移除设备注册表条目
                    service_router.register_device_to_hilink(vd.sn, 2)
                    vd.online = False
                    _remove_bridge_device_entry_async(vd.sn)
                    _LOGGER.info(f"Device unregistered via options flow: sn={vd.sn}")
                else:
                    # 勾选 → 注册设备（status=3 + status=1）+ 创建设备注册表条目
                    service_router.register_device_to_hilink(vd.sn, 3)
                    service_router.register_device_to_hilink(vd.sn, 1)
                    vd.online = True
                    _create_bridge_device_entry_async(vd, name, model, manufacturer)
                    _LOGGER.info(f"Device registered via options flow: sn={vd.sn}")
            
            if selected_sns:
                # 进入实体管理：为每个选中设备配置 service→entity 映射
                return await self.async_step_entity_map(selected_sns=sorted(selected_sns))
            return self.async_create_entry(title="", data={})
        
        # 显示设备选择表单
        return self.async_show_form(
            step_id="select_device",
            data_schema=vol.Schema({
                vol.Required("devices", default=default_selected): cv.multi_select(options_dict),
            }),
            description_placeholders={"device_count": str(len(all_vds))},
        )
    
    def _entity_map_form(self, selected_sns):
        """构建实体映射表单：返回 (schema_items, key_map, legend)

        schema_items: {key: SelectSelector}
        key_map:      {key: (sn, svc_id, default_entity)}
        """
        from .hwbridge import service_router

        ent_reg = er.async_get(self.hass)
        dev_reg = dr.async_get(self.hass)
        sn_manager = service_router.sn_manager

        schema_items = {}
        key_map = {}
        legend = []
        used_names = set()
        for sn in selected_sns:
            vd = sn_manager.get_by_sn(sn)
            if vd is None:
                continue
            dev = dev_reg.async_get(vd.ha_device_id)
            dev_name = ((dev.name_by_user or dev.name) if dev is not None
                        else None) or (vd.product.name if vd.product
                                       else None) or sn
            base = dev_name
            if base in used_names:
                dev_name = f"{base} ({sn[:6]})"
            used_names.add(base)

            entities = [
                ent for ent in ent_reg.entities_for_device(vd.ha_device_id)
                if getattr(ent, "platform", None) not in SKIP_PLATFORMS
            ]
            candidates = sorted(ent.entity_id for ent in entities)
            legend.append(f"{dev_name}：{', '.join(candidates) or '（无实体）'}")
            for svc_id, svc_def in (vd.product.services or {}).items():
                key = f"{dev_name} · {svc_id}"
                key_map[key] = (sn, svc_id)
                schema_items[key] = selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            {"value": "__disable__", "name": "（不映射）"}
                        ] + [
                            {"value": eid, "name": eid}
                            for eid in candidates
                        ],
                        mode=selector.SelectMode.DROPDOWN,
                        custom_value=False,
                    )
                )
        return schema_items, key_map, legend

    async def async_step_entity_map(self, user_input=None, selected_sns=None):
        """实体管理：为每个选中设备选择 service→entity 映射

        一个 HA 设备（如两按键开关）可能包含多个同类型实体，
        默认自动映射只取一个。此步骤允许为产品的每个服务
        指定具体实体，或选择"不映射"禁用该服务。

        保存后受影响设备立即重新注册并同步状态，
        华为智慧生活APP按新映射显示对应实体。
        """
        from .hwbridge import service_router, refresh_bridge_device

        if service_router is None:
            return self.async_abort(reason="integration_not_init")

        if user_input is None:
            schema_items, key_map, legend = self._entity_map_form(selected_sns)
            if not schema_items:
                return self.async_create_entry(title="")
            schema = {}
            for key, sel in schema_items.items():
                sn, svc_id = key_map[key]
                vd = service_router.sn_manager.get_by_sn(sn)
                cur = vd.service_entries.get(svc_id)
                schema[vol.Required(key, default=(
                    cur.entity_id if cur else "__disable__"))] = sel
            return self.async_show_form(
                step_id="entity_map",
                data_schema=vol.Schema(schema),
                description_placeholders={
                    "legend": "\n\n".join(legend) if legend
                              else "（无选中设备）",
                },
            )

        # 用户提交：解析、持久化、重新注册受影响设备
        schema_items, key_map, _ = self._entity_map_form(selected_sns)
        existing = dict(self.config_entry.data.get("entity_map") or {})
        changed_devices = []
        for key, (sn, svc_id) in key_map.items():
            vd = service_router.sn_manager.get_by_sn(sn)
            if vd is None:
                continue
            dev_id = vd.ha_device_id
            val = user_input.get(key)
            val = None if val == "__disable__" else val
            em = existing.setdefault(dev_id, {})
            if em.get(svc_id) != val:
                em[svc_id] = val
                if dev_id not in changed_devices:
                    changed_devices.append(dev_id)

        if changed_devices:
            data = dict(self.config_entry.data)
            data["entity_map"] = existing
            self.hass.config_entries.async_update_entry(
                self.config_entry, data=data)
            for dev_id in changed_devices:
                _LOGGER.info(f"Re-registering device with new entity map: {dev_id}")
                refresh_bridge_device(dev_id)

        return self.async_create_entry(title="")

    async def _wait_for_binding(self, pin_code: int):
        """后台任务：等待用户在APP上完成绑定"""
        start_time = time.time()
        
        while True:
            await asyncio.sleep(2)
            
            # 检查是否超时
            if time.time() - start_time > self.__DEFAULT_TIME_OUT:
                raise TimeoutError("PIN expired")
            
            # 检查PIN是否仍然有效
            current_pin = PINManager.get_current_pin()
            if current_pin is None:
                # PIN已被清除
                _LOGGER.warning("PIN was cleared during binding")
                raise TimeoutError("PIN expired")

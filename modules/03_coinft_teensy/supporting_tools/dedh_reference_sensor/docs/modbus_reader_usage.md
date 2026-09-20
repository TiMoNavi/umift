# DEDH Modbus 读取脚本

脚本路径：

- [../scripts/dedh_modbus_reader.py](../scripts/dedh_modbus_reader.py)

## 当前脚本能做什么

### 1. 读取 6 轴浮点值

按照说明书里的这一组命令实现：

```text
01 03 04 00 00 0C ...
```

也就是：

- 起始地址：`F地址 1024`
- 连续读取：`12` 个 16-bit 寄存器
- 解析结果：`Fx/Fy/Fz/Mx/My/Mz`
- 格式：`big-endian float32`

### 2. 读取 6 轴整型值

按照说明书里的这一组命令实现：

```text
01 03 0A 00 00 0C ...
```

也就是：

- 起始地址：`L地址 2560`
- 连续读取：`12` 个 16-bit 寄存器
- 解析结果：6 个 `int32`
- 缩放：`/100`

### 3. 所有通道清零

按照说明书里的“多功能码 27”实现：

```text
向 1574 地址写入 27
```

### 4. 启动主动发送协议2

按照说明书里的命令实现：

```text
向特殊地址 410 写入 00 03
```

注意：

- 这个命令只负责切换模式
- 当前脚本还不解析主动发送协议2的数据帧
- 一旦切过去，说明书要求重新上电才能回到 `Modbus-RTU`

## 依赖

当前只依赖：

- `pyserial`

安装：

```bash
python -m pip install pyserial
```

## 最小使用方式

### 连续读 20 个样本

```bash
python modules/03_coinft_teensy/supporting_tools/dedh_reference_sensor/scripts/dedh_modbus_reader.py \
  --port /dev/cu.usbserial-0001 \
  stream \
  --samples 20
```

### 边读边写 CSV

```bash
python modules/03_coinft_teensy/supporting_tools/dedh_reference_sensor/scripts/dedh_modbus_reader.py \
  --port /dev/cu.usbserial-0001 \
  stream \
  --duration 10 \
  --csv modules/03_coinft_teensy/supporting_tools/dedh_reference_sensor/data/dedh_probe.csv
```

### 先清零再开始读

```bash
python modules/03_coinft_teensy/supporting_tools/dedh_reference_sensor/scripts/dedh_modbus_reader.py \
  --port /dev/cu.usbserial-0001 \
  stream \
  --zero-first \
  --samples 20
```

### 单独执行全部通道清零

```bash
python modules/03_coinft_teensy/supporting_tools/dedh_reference_sensor/scripts/dedh_modbus_reader.py \
  --port /dev/cu.usbserial-0001 \
  zero-all
```

### 切到主动发送协议2

```bash
python modules/03_coinft_teensy/supporting_tools/dedh_reference_sensor/scripts/dedh_modbus_reader.py \
  --port /dev/cu.usbserial-0001 \
  start-active-upload
```

## 当前实现边界

这版脚本故意只做“链路验证级别”工作，不冒进：

- 不假设 vendor GUI 存在
- 不假设主动发送帧格式已经整理干净
- 不假设它和 CoinFT 已经完成时间同步

所以它现在最适合回答的问题是：

```text
USB-RS485 接好了没有
默认 115200 / 地址 1 能不能读通
六轴数据有没有出来
清零命令是否生效
```

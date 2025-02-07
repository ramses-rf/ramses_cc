![ruff](https://github.com/zxdavb/ramses_cc/actions/workflows/check-lint.yml/badge.svg?master)
![mypy](https://github.com/zxdavb/ramses_cc/actions/workflows/check-type.yml/badge.svg?master)
![pytest](https://github.com/zxdavb/ramses_cc/actions/workflows/check-test.yml/badge.svg?master)

# New code owner wanted
I no longer am able to work on this project and active development has ceased.

Please reach out to me if you feel able to take over. I will hand over the entire repo to the right person.

## Overview
**ramses_cc** is a HA custom component that works with RAMSES II-based RF 868 Mhz systems for (heating) **CH/DHW** (e.g. Honeywell Evohome) and (ventilation) **HVAC** (e.g. Itho Spider).

This includes CH/DHW systems such as **evohome**, **Sundial**, **Hometronic**, **Chronotherm** and others.

The simplest way to know if it will work with your CH/DHW system is to identify the box connected to your boiler (or other heat source) to one of (there will be other systems that also work):
 - **R8810A** or **R8820A**: OpenTherm Bridge
 - **BDR91A** or **BDR91T**: Wireless Relay
 - **HC60NG**: Wireless Relay (older hardware version)

It also works with HVAC (ventilation) systems using the same protocol, such as from **Itho**, **Orcon**, **Nuaire**, **Ventiline**, **Vasco**, etc.

It uses the [ramses_rf](https://github.com/zxdavb/ramses_rf) client library to decode the RAMSES-II protocol used by these devices. Note that other systems, such as HVAC, also use this protocol, YMMV.

It requires a USB-to-RF device, either a Honeywell HGI80 (rare, expensive) or something running [evofw3](https://github.com/ghoti57/evofw3), such as the one from [here](https://indalo-tech.onlineweb.shop/).

See the [wiki](https://github.com/zxdavb/ramses_cc/wiki) for installation, configuration, troubleshooting, etc.

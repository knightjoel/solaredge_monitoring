# Multi-inverter SolarEdge system monitoring - build runbook

This runbook describes how to build an rpi for monitoring a SolarEdge solar panel installation. The rpi will gather, store, and present metric data about the solar panel system.

Lots of assets in this runbook were reused from
[Nelis Oosten's excellent blog post](https://oostens.me/posts/solaredge-se3000h-monitoring/). While Nelis was using a single inverter system, this runbook is designed for multi-inverter systems.

For more context and background, please see my blog post
[Monitoring a Multi-Inverter SolarEdge System](https://www.packetmischief.ca/2022/01/09/monitoring-a-multi-inverter-solaredge-system/).

# Pre-requisites

- A Raspberry Pi running [Raspberry Pi OS](https://www.raspberrypi.com/software/)
- Each inverter is connected to the network with Modbus/TCP enabled.
- A clone of this repo stored in `/home/pi/solaredge_monitoring`

```
cd /home/pi
git clone https://github.com/knightjoel/solaredge_monitoring
```

# Assumptions

- The `pi` user is used on the rpi to execute the commands in this runbook.
- The `pi` user's home directory has not been changed from the default of `/home/pi`.

# OS configuration

Set the time zone using `raspi-config` (Localisation Options)

# Security hardening

- Change the `pi` user password:

```
passwd
```

# Prep and install containers

For ease of use, [IOTstack](https://github.com/SensorsIot/IOTstack)—a Docker stack for getting started on IOT on the Raspberry PI—is used to create the Docker Swarm configuration.

Clone IOTstack into `/home/pi/IOTstack`:

```
git clone https://github.com/SensorsIot/IOTstack.git ~/IOTstack
```

Create the customization yaml for IOTstack:

```
cp ~/solaredge_monitoring/IOTstack/compose-override.yml ~/IOTstack
```

Apply the diff to the telegraf Dockerfile. The diff provides the necessary commands to install python3 inside the telegraf container which is needed to run the `solarEdgeCloudScraper.py` script later on.

```
cd ~/IOTstack
patch < ~/solaredge_monitoring/IOTstack/Dockerfile-telegraf.diff
```

Run the IOTstack menu to configure IOTstack:

```
cd ~/IOTstack
./menu.sh
```

Choose `Yes` to install all dependencies (python, python modules, docker, docker-compose, etc).

If you receive an error `Error getting docker version. Received permission denied error. Try running with: ./menu.sh --run-env-setup`, add the `pi` user to the `docker` group. In my experience, the `--run-env-setup` argument is broken and doesn't work.

```
sudo usermod -G "docker" -a pi
```

Choose containers

- Grafana
    - Change external port to `80`
- InfluxDB
- Telegraf

From the `Miscellaneous Commands` menu, select:

- Uninstall swapfile
- Install log2ram

[Upgrade libseccomp2](https://sensorsiot.github.io/IOTstack/Getting-Started/#patch-2-update-libseccomp2) to avoid a bug that is triggered for any container that is based on Alpine 3.13:

```
$ sudo apt-key adv --keyserver hkps://keyserver.ubuntu.com:443 --recv-keys 04EE7237B7D453EC 648ACFD622F3D138
$ echo "deb http://httpredir.debian.org/debian buster-backports main contrib non-free" | sudo tee -a "/etc/apt/sources.list.d/debian-backports.list"
$ sudo apt update
$ sudo apt install libseccomp2 -t buster-backports
```

# Configure Telegraf

Overwrite the supplied Telegraf config file with the one from this repo.

```
cp ~/solaredge_monitoring/telegraf/telegraf.conf ~/IOTstack/volumes/telegraf
```

Install the script which pulls data from the SolarEdge API:

```
cp ~/solaredge_monitoring/telegraf/solarEdgeCloudScarper.py ~/IOTstack/volumes/telegraf
```

- Make the script executable and not publicly readable `chmod 700 ~/IOTstack/volumes/telegraf/solarEdgeCloudScarper.py`
- Edit the script and fill in `SETTING_API_KEY` with your SolarEdge web portal API key. The key is configurable within the portal under Admin, Site Access.
- Still within the script, fill in the `SETTING_SITE_USERNAME` and `SETTING_SITE_PW` with your username and password, respectively, for the SolarEdge web portal.

Edit `~/IOTstack/volumes/telegraf/telegraf.conf` and customize it for your SolarEdge system:

- The supplied `telegraf.conf` is configured for 2 inverters. If you have more or less than 2 inverters, you should duplicate/remove an `[[inputs.modbus]]` section in the file. Each inverter requires its own input section.
- For each `[[inputs.modbus]]` section in the file, modify these parameters:
	- `name_override`: The name of the measurement in InfluxDB for the inverter. Use the format `inverterX` where `X` is an integer. eg, `inverter1`, `inverter7`, `inverter13`, etc. The Grafana dashboard is configured to query measurements which follow this format.
	- `controller`: The IP address and Modbus/TCP port for the inverter. eg, `tcp://192.168.0.250:1502`
	- Within the `[inputs.modbus.tags]` section, modify these parameters:
		- `site`: Your SolarEdge Site ID (as found on the SolarEdge website once you log in).
		- `sn`: The serial number of the inverter as found on the sticker on the side of the inverter or via the inverter's web UI.

# Launch the stack

Launch the stack:

```
cd ~/IOTstack
docker-compose up -d
```

# Configure InfluxDB

Connect to InfluxDB using the `influx` command.

```
docker exec -it influxdb influx
```

Create the databases by pasting the contents of [create.sql](influxdb/create.sql) into the influx shell. Three databases will be created:

1. For the data queried from the inverters using Modbus/TCP.
2. For the data retrieved from the SolarEdge cloud.
3. For the data queried from Docker and the rpi (memory, CPU, etc).

Create the data retention policies by pasting the contents of [retention.sql](influxdb/retention.sql) into the influx shell.

Exit the influx shell by pressing ctrl+d.

# Configure Grafana

Log into the Grafana UI at http://<rpi_ip_address>:80

- Username: `admin`
- Password: `admin`

Set admin password when prompted.

Install these Grafana plugins by browsing to Configuration, Plugins, searching for each plugin, and clicking `Install`:

- Sun and Moon

Add these datasources by browsing to Configuration, Data sources, and clicking `Add data source`:

- InfluxDB-rpi
    - URL: http://influxdb:8086/
    - Database: `rpi`
- InfluxDB-solaredge_cloud
    - URL: http://influxdb:8086/
    - Database: `solaredge\_cloud`
    - Query language: Flux
- InfluxDB-solaredge
    - URL: http://influxdb:8086/
    - Database: `solaredge`
    - Query language: Flux
- Sun and Moon
    - Long: <your longitude in decimal>
    - Lat: <your latitude in decimal>

Import the SolarEdge dashboards by browsing to Dashboards, Manage, and clicking `Import`. Paste the contents of the first dashboard file below into the `Import via panel json` box and click `Load`. Repeat for the rest of the dashboards.

- [SolarEdge real-time dashboard](grafana/dashboard_solaredge_realtime.json)
- [SolarEdge cloud dashboard](grafana/dashboard_solaredge_cloud.json)

Install the Docker and PiServer dashboards by browsing to Dashboards, Manage, and clicking `Import`. Paste the URL of the first dashboard below into the `Import via grafana.com` box and click `Load`. Repeat for the rest of the dashboards.

- Docker: [https://grafana.com/grafana/dashboards/5763](https://grafana.com/grafana/dashboards/5763)
- PiServer monitoring: [https://grafana.com/grafana/dashboards/13044](https://grafana.com/grafana/dashboards/13044)

💡 The dashboards don't show disk IO, memory usage per container, or host network interface stats, possibly the result of not running the telegraf container in privileged mode. See [https://github.com/influxdata/telegraf/tree/master/plugins/inputs/diskio](https://github.com/influxdata/telegraf/tree/master/plugins/inputs/diskio).

# Import historical SolarEdge data

Import historical SolarEdge data by scraping the SolarEdge API. This is a one-time operation and should not need to be repeated once the data is successfully imported. In my experience, I did run this multiple times due to occasional API throttles and just to make sure I had the most recent data ingested.

Copy the "one time" telegraf config file into the telegraf container:

```
cp ~/solaredge_monitoring/telegraf/telegraf-onetime-solaredge-history.conf ~/IOTstack/volumes/telegraf
```

Import the data using telegraf:

```
docker exec -it telegraf /bin/bash
telegraf --once --config /etc/telegraf/telegraf-onetime-solaredge-history.conf
```

💡 For reasons I haven't yet root-caused, this script appears to miss some data. In multiple runs, it always missed a specific day of the month (Why? No data on that day? Doubtful) and it always missed "yesterday". Don't be surprised if the data is lumpy after importing the history.

# Wrap up

At this point everything is ready and should be working.

- Historical data has been pulled into the database and should be visible on the dashboard.
- Telegraf is polling the inverters for recent, (near) real-time data which should also be visible in the dashboard.
- Once a day, Telegraf is ingesting data from the SolarEdge cloud via solarEdgeCloudScraper.py. This is mostly useful for the per-optimizer data which the cloud collects but is not available via Modbus.

Depending how many inverters are in the system, you may need to rearrange the Grafana dashboards so everything fits.


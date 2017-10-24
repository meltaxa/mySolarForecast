#!/usr/bin/env python
#
# mySolarForecast will provide two daily PV forecasts (kWh) from Solcast
# and ASEFS. Both forecasters give half hourly or hourly forecasts. This
# script will sum these up to provide the daily forecast and send this to
# an InfluxDB database.
# 

import boto3
import solcast
from dateutil import tz
import datetime
from influxdb import InfluxDBClient
import urllib3
import os
import ast
from bs4 import BeautifulSoup
from zipfile import ZipFile
from StringIO import StringIO
import urllib2
import urllib3
from urllib import urlopen
import csv

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from_zone = tz.gettz('UTC')
to_zone = tz.gettz('Australia/Brisbane')

today_fmt = datetime.datetime.strftime(datetime.date.today(), "%Y/%m/%d")
tomoz = datetime.date.today() + datetime.timedelta(days=1)
tomoz_fmt = datetime.datetime.strftime(tomoz, "%Y/%m/%d")

def get_param_store(name):
    ssm = boto3.client('ssm')
    try:
        params = ssm.get_parameters(Names=[name])
    except ValueError:
        raise
    if len(params.get('Parameters', [])) != 1:
        return "nil"
    try:
        value = params.get('Parameters')[0].get('Value', '')
    except ValueError as err:
        print "Invalid paramter given: %s", err
        raise
    return value

def update_param_store(name, value):
    ssm = boto3.client('ssm')
    try:
        params = ssm.put_parameter(
                   Name=name,
                   Value=value,
                   Type="String",
                   Overwrite=True)
    except ValueError:
        raise  

def get_solcast(capacity,azimuth):
#
# Forecasts are summed and halved due to the 30 minute forecast increments.
#
  pvcast = solcast.get_pv_power_forecasts(
             os.environ['long'], 
             os.environ['lat'], 
             capacity=capacity, 
             azimuth=azimuth, 
             api_key=os.environ['solcast_api_key'])
  forecast_date = {}
  for period in pvcast.forecasts:
    period_end = period['period_end']
    period_end = period_end.replace(tzinfo=from_zone)
    period_tz = period_end.astimezone(to_zone)
    pv_estimate = period['pv_estimate']
    pv_date = datetime.datetime.strftime(period_tz, '%Y/%m/%d')
    try:
      forecast_date[pv_date] += float(pv_estimate) / 1000
    except:
      forecast_date[pv_date] = float(pv_estimate)/ 1000
  return forecast_date

url = "http://www.nemweb.com.au/Reports/Current/ROOFTOP_PV/FORECAST"
html = urllib2.urlopen(url).read()
soup = BeautifulSoup(html, "html.parser")

def get_asefs():
#
# Download and unzip the latest ASEFS2 report and get the forecast power mean.
# Assumptions made: Rooftop PV is a 3K system. This script converts it to a
# 5K forecast.
# 
#
  last_time = get_param_store("lambda.last_time")
  last_link = soup.find_all('a', href=True)[-1]
  if last_link['href'] == last_time:
    return "nil"
  update_param_store("lambda.last_time", last_link['href'])
  url = "http://www.nemweb.com.au" + last_link['href']
  url = urlopen(url)
  zipfile = ZipFile(StringIO(url.read()))
  for file in zipfile.namelist():
    forecast_date = {}
    for line in zipfile.open(file):
      reader = csv.reader([line])
      for r in reader:
        if len(r) == 12 and r[5] == "QLD1":
          dt = datetime.datetime.strptime(r[6], "%Y/%m/%d %H:%M:%S")
          dt_fmt = datetime.datetime.strftime(dt, "%Y/%m/%d")
          try:
            forecast_date[dt_fmt] += float(r[7]) / 1000 * 1.666
          except:
            forecast_date[dt_fmt] = float(r[7]) / 1000 * 1.666
  return forecast_date

def lambda_handler(event, context):
  flux_client = InfluxDBClient(
                  os.environ['influxdb_ip'],
                  int(os.environ['influxdb_port']),
                  os.environ['influxdb_user'],
                  os.environ['influxdb_password'],
                  os.environ['influxdb_database'],
                  ssl=ast.literal_eval(os.environ['influxdb_ssl']),
                  verify_ssl=ast.literal_eval(os.environ['influxdb_verify_ssl']))
  
  total = 0
  total_today = 0
  for pair in ast.literal_eval(os.environ['pv_roof']):
    forecasts = get_solcast(pair[1],pair[0])
    total += forecasts[tomoz_fmt]
    total_today += forecasts[today_fmt]
  total = total / 2
  total_today = total_today / 2
  metrics = {}
  tags = {}
  fields = {}
  metrics['measurement'] = os.environ['influxdb_measurement']
  tags['location'] = os.environ['influxdb_location']
  metrics['tags'] = tags
  pv_forecast = {}
  pv_forecast['pv_solcast'] = total
  pv_forecast['pv_solcast_today'] = total_today
  metrics['fields'] = pv_forecast
  print tomoz, total_today, total
  flux_client.write_points([metrics])
  print "[INFO] Sent Solcast to InfluxDB"

  forecast_date = get_asefs()
  if forecast_date != "nil":
    for key in forecast_date.keys():
      print key,forecast_date[key]
      if key == tomoz_fmt:
        print "Tomorrow:"
        print key,forecast_date[key]
        pv_forecast['pv_forecast'] = forecast_date[key]
        metrics['fields'] = pv_forecast
        flux_client.write_points([metrics])
        print "[INFO] Sent ASEFS to InfluxDB"

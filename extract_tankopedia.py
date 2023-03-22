#!/usr/bin/env python3
## PYTHON VERSION MUST BE 3.7 OR HIGHER

# Extract tankopedia data in WG API JSON format from Blitz app files (Android APK unzipped)

from os import path
import aiofiles 				# type: ignore
import xmltodict				# type: ignore
from asyncio import set_event_loop_policy, run, create_task, get_event_loop_policy
import sys, argparse, json, os, inspect, asyncio, re, logging, time, configparser
from collections import OrderedDict
import blitzutils as bu
from blitzutils import WG

logging.getLogger("asyncio").setLevel(logging.DEBUG)

FILE_CONFIG 	= 'blitzstats.ini'

# BLITZAPP_STRINGS='/assets/Data/Strings/en.yaml'
# BLITZAPP_VEHICLES_DIR='/assets/Data/XML/item_defs/vehicles/'
BLITZAPP_STRINGS='Data/Strings/en.yaml'
BLITZAPP_VEHICLES_DIR='Data/XML/item_defs/vehicles/'
BLITZAPP_VEHICLE_FILE='list.xml'

wg : WG | None = None

## main() -------------------------------------------------------------
async def main(argv: list[str]) -> None:
	global wg
	# set the directory for the script
	os.chdir(os.path.dirname(sys.argv[0]))

	## Read config
	BLITZAPP_FOLDER = '.'
	try:
		if os.path.isfile(FILE_CONFIG):
			config = configparser.ConfigParser()
			config.read(FILE_CONFIG)

			configOptions 	= config['EXTRACT_TANKOPEDIA']
			BLITZAPP_FOLDER = configOptions.get('blitz_app_dir', BLITZAPP_FOLDER)
	except:
		pass
	
	parser = argparse.ArgumentParser(description='Extract Tankopedia data from Blitz game files')
	parser.add_argument('blitz_app_base', type=str, nargs='?', metavar="BLITZAPP_FOLDER", default=BLITZAPP_FOLDER, help='Base dir of the Blitz App files')
	parser.add_argument('tanks', type=str, default='tanks.json', nargs='?', metavar="TANKS_FILE", help='File to write Tankopedia')
	parser.add_argument('maps', type=str, default='maps.json', nargs='?', metavar='MAPS_FILE', help='File to write map names')
	arggroup = parser.add_mutually_exclusive_group()
	arggroup.add_argument('-d', '--debug', action='store_true', default=False, help='Debug mode')
	arggroup.add_argument('-v', '--verbose', action='store_true', default=False, help='Verbose mode')
	arggroup.add_argument('-s', '--silent', action='store_true', default=False, help='Silent mode')
		
	args = parser.parse_args()
	bu.set_log_level(args.silent, args.verbose, args.debug)

	wg = WG()
	
	tasks = []
	for nation in wg.NATION:
		tasks.append(asyncio.create_task(extract_tanks(args.blitz_app_base, nation)))

	tanklist = []
	for tanklist_tmp in await asyncio.gather(*tasks):
		tanklist.extend(tanklist_tmp)
	
	tank_strs, map_strs = await read_user_strs(args.blitz_app_base)

	json_data = None
	userStrs = {}
	tanks = {}
	if os.path.exists(args.tanks):
		try:
			async with aiofiles.open(args.tanks) as infile:
				json_data = json.loads(await infile.read())
				userStrs = json_data['userStr']
				tanks = json_data['data']
		except Exception as err:
			bu.error('Unexpected error when reading file: ' + args.tanks + ' : ' + str(err))

	async with aiofiles.open(args.tanks, 'w', encoding="utf8") as outfile:
		new_tanks, new_userStrs = await convert_tank_names(tanklist, tank_strs)
		# merge old and new tankopedia
		tanks.update(new_tanks)
		userStrs.update(new_userStrs) 
		tankopedia : OrderedDict[str, str| int | dict ]= OrderedDict()
		tankopedia['status'] = 'ok'
		tankopedia['meta'] = { "count":  len(tanks) }
		tankopedia['data'] = bu.sort_dict(tanks, number=True)
		tankopedia['userStr'] = bu.sort_dict(userStrs)
		bu.verbose_std('New tankopedia \'' + args.tanks + '\' contains ' + str(len(tanks)) + ' tanks')
		bu.verbose_std('New tankopedia \'' + args.tanks + '\' contains ' + str(len(userStrs)) + ' tank strings')        
		await outfile.write(json.dumps(tankopedia, ensure_ascii=False, indent=4, sort_keys=False))
	
	if args.maps is not None:
		maps = {}
		if os.path.exists(args.maps):
			try:
				async with aiofiles.open(args.maps) as infile:
					maps = json.loads(await infile.read())
			except Exception as err:
				bu.error('Unexpected error when reading file: ' + args.maps + ' : ' + str(err))
		# merge old and new map data
		maps.update(map_strs)
		async with aiofiles.open(args.maps, 'w', encoding="utf8") as outfile:
			bu.verbose_std('New maps file \'' + args.maps + '\' contains ' + str(len(maps)) + ' maps')
			await outfile.write(json.dumps(maps, ensure_ascii=False, indent=4, sort_keys=True))

	return None
	
async def extract_tanks(blitz_app_base : str, nation: str):

	tanks = []
	# WG has changed the location of Data directory - at least in steam client
	if path.isdir(path.join(blitz_app_base, 'assets')):
		blitz_app_base = path.join(blitz_app_base, 'assets')
	list_xml_file = path.join(blitz_app_base, BLITZAPP_VEHICLES_DIR ,nation ,BLITZAPP_VEHICLE_FILE)
	if not path.isfile(list_xml_file): 
		print('ERROR: cannot open ' + list_xml_file)
		return None
	bu.debug('Opening file: ' + list_xml_file + ' (Nation: ' + nation + ')')
	async with aiofiles.open(list_xml_file, 'r', encoding="utf8") as f:
		try: 
			tankList = xmltodict.parse(await f.read())
			for data in tankList['root'].keys():
				tank_xml = tankList['root'][data]
				tank : dict[str, bool | int | str]= dict()
				tank['is_premium'] = issubclass(type(tank_xml['price']), dict)
				tank['nation']  = nation
				tank['tank_id'] = get_tank_id(nation, int(tank_xml['id']))
				tank['tier']    = int(tank_xml['level'])
				tank['type']    = get_tank_type(tank_xml['tags'])
				tank['userStr'] = tank_xml['userString']
				#bu.debug('Reading tank string: ' + tank['userStr'], force=True)
				tanks.append(tank)
		except Exception as err:
			bu.error(err)
			sys.exit(2)
	return tanks

async def read_user_strs(blitz_app_base : str) -> tuple[dict, dict]:
	"""Read user strings to convert map and tank names"""
	tank_strs = {}
	map_strs = {}
	filename = path.join(blitz_app_base, BLITZAPP_STRINGS)
	bu.debug('Opening file: ' + filename + ' for reading UserStrings')
	try:
		async with aiofiles.open(filename, 'r', encoding="utf8") as f:
			p_tank = re.compile('^"(#\\w+?_vehicles:.+?)": "(.+)"$')
			p_map = re.compile('^"#maps:(.+?):.+?: "(.+?)"$')
			
			async for l in f:
				m = p_tank.match(l)
				if m is not None: 
					tank_strs[m.group(1)] = m.group(2)
				
				m = p_map.match(l)
				if m is not None and m.group(2) != 'Macragge':                    
					map_strs[m.group(1)] = m.group(2)   
	
	except Exception as err:
		bu.error(err)
		sys.exit(1)

	return tank_strs, map_strs
	
async def convert_tank_names(tanklist : list, tank_strs: dict) -> tuple[dict, dict]:
	"""Convert tank names for Tankopedia"""
	tankopedia = {}
	userStrs = {}

	bu.debug(f'tank_strs:')
	for key, value in tank_strs.items():
		bu.debug(f'{key}: {value}')
	bu.debug('---------')
	try:
		for tank in tanklist:
			try:
				bu.debug(f'tank: {tank}')
				if tank['userStr'] in tank_strs:
					tank['name'] = tank_strs[tank['userStr']]
				else:
					tank['name'] = tank['userStr'].split(':')[1]
				tank.pop('userStr', None)
				tank_tmp = OrderedDict()
				for key in sorted(tank.keys()):
					tank_tmp[key] = tank[key]
				tankopedia[str(tank['tank_id'])] = tank_tmp
			except:
				bu.error(f'Could not process tank: {tank}')

		for tank_str in tank_strs:
			skip = False
			key = tank_str.split(':')[1]
			bu.debug('Tank string: ' + key + ' = ' + tank_strs[tank_str])
			re_strs = [r'^Chassis_', r'^Turret_', r'^_', r'_short$' ]
			for re_str in re_strs:
				p = re.compile(re_str)
				if p.match(key):
					skip = True
					break
			if skip:
				continue
			
			userStrs[key] = tank_strs[tank_str]

		# sorting
		tankopedia_sorted = OrderedDict()
		for tank_id in sorted(tankopedia.keys(), key=int):
			tankopedia_sorted[str(tank_id)] = tankopedia[str(tank_id)]

		userStrs_sorted = OrderedDict()
		for userStr in sorted(userStrs.keys()):
			userStrs_sorted[userStr] = userStrs[userStr]
		# bu.debug('Tank strings: ' + str(len(userStrs_sorted)))

	except Exception as err:
		bu.error(err)
		sys.exit(1)

	return tankopedia_sorted, userStrs_sorted


def get_tank_id(nation: str, tank_id : int) -> int:
	assert wg is not None, "wg has to be set"
	return (tank_id << 8) + (wg.NATION_ID[nation] << 4) + 1 


def get_tank_type(tagstr : str):
	assert wg is not None, "wg has to be set"
	tags = tagstr.split(' ')
	for t_type in wg.TANK_TYPE:
		if tags[0] == t_type:
			return t_type
	return None

### main()
if __name__ == "__main__":
	# To avoid 'Event loop is closed' RuntimeError due to compatibility issue with aiohttp
	if sys.platform.startswith("win") and sys.version_info >= (3, 8):
		try:
			from asyncio import WindowsSelectorEventLoopPolicy
		except ImportError:
			pass
		else:
			if not isinstance(get_event_loop_policy(), WindowsSelectorEventLoopPolicy):
				set_event_loop_policy(WindowsSelectorEventLoopPolicy())
	#asyncio.run(main(sys.argv[1:]), debug=True)
	run(main(sys.argv[1:]))

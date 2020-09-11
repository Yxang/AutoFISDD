import socket

config = {}
config['data_path'] = '~/jupyter/data/NAS_dataset/'  #add your path here --sys.path.append(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))



host = socket.gethostname()
config['host'] = host.lower()

config['env'] = 'gpu'
config['dtype'] = 'float32'
config['scale'] = 0.001
config['minval'] = - config['scale']
config['maxval'] = config['scale']
config['mean'] = 0
config['stddev'] = 0.001
config['sigma'] = config['stddev']
config['const_value'] = 0
config['rnd_type'] = 'uniform'
config['factor_type'] = 'avg'
config['magnitude'] = 3


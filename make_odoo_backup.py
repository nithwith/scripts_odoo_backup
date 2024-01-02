import xmlrpc.client
import os, time, logging, subprocess
import argparse
from dotenv import load_dotenv
import paramiko
from scp import SCPClient

load_dotenv()
ODOO_URL = os.getenv('ODOO_URL')
ODOO_DB = os.getenv('ODOO_DB')
ODOO_USERNAME = os.getenv('ODOO_USERNAME')
ODOO_PASSWORD = os.getenv('ODOO_PASSWORD')
BACKUP_PATH = os.getenv('BACKUP_PATH')
SYNOLOGY_URL = os.getenv('SYNOLOGY_URL')
SYNOLOGY_USERNAME = os.getenv('SYNOLOGY_USERNAME')
SYNOLOGY_PASSWORD = os.getenv('SYNOLOGY_PASSWORD')
now = time.time()

def get_file_params():
    argParser = argparse.ArgumentParser()
    argParser.add_argument("-p", "--period", help="Period of your backup")
    args = argParser.parse_args()
    if args.period in ['daily','monthly']:
        backup_type = args.period
    else:
        raise Exception("You need to send the period of the backup (daily or monthly) with -p argument")
    return backup_type
    
def get_db_to_backup():
    common = xmlrpc.client.ServerProxy('{}/xmlrpc/2/common'.format(ODOO_URL))
    uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})
    models = xmlrpc.client.ServerProxy('{}/xmlrpc/2/object'.format(ODOO_URL))
    ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'project.task', 'search', [[('tag_ids.name', '=', "To backup")]],)
    return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD,'project.task', 'read', [ids], {'fields': ['name']})

def create_logfile():
    log_path = BACKUP_PATH + "/log"
    os.makedirs(log_path, exist_ok=True)
    if not os.path.exists(log_path + "/daily_backup.log"):
        open(log_path + "/daily_backup.log", 'w').close()
    logging.basicConfig(filename=log_path + "/daily_backup.log",
                        filemode="w",
                        format='%(asctime)s %(levelname)-8s %(message)s',
                        level=logging.INFO,
                        datefmt='%Y-%m-%d %H:%M:%S')
    return logging.getLogger()

def make_backup(db_info, backup_type):
    if not os.path.isdir(db_info['backup_root_path']):
        os.makedirs(db_info['backup_root_path'])

    # db_info['backup_root_path'] = db_info['backup_root_path'] + "/"
    backup_path = db_info['backup_root_path'] + '%s_%s.zip' % (db_info['backup_db_url'], time.strftime('%Y_%m_%d_%H_%M_%S'))
    backup_url = 'https://'+db_info['backup_db_url']+"/web/database/backup"

    subprocess.run(["curl", "-X", "POST", '-F', 'master_pwd=pPaJncYL8MgqSt', '-F', 'name=odoo', '-F', 'backup_format=zip', '-o', 
                    backup_path, backup_url],stdout=subprocess.DEVNULL,stderr=subprocess.STDOUT)
    logger.info('Let s %s Backup %s' % (backup_type, db_info['backup_db_url']))


    if subprocess.run(["curl", "-X", "POST", '-F', 'master_pwd=pPaJncYL8MgqSt', '-F', 'name=odoo' '-F', 'backup_format=zip', '-o', 
                       backup_path, backup_url],stdout=subprocess.DEVNULL,stderr=subprocess.STDOUT).returncode == 0:
        logger.info('End of %s %s Backup' % (backup_type, db_info['backup_db_url']))
        return db_info
    
def remove_old_backup(db_info,backup_type):
    logger.info('Remove old %s backup of %s' % (backup_type, db_info['backup_db_url']))
    for filename in os.listdir(db_info['backup_root_path']):
        filestamp = os.stat(os.path.join(db_info['backup_root_path'], filename)).st_mtime
        if backup_type == "daily":
            critical_time = now - 5 * 86400 #5 days
        else:
            critical_time = now - 155 * 86400 #5 month
        if filestamp <  critical_time:
            os.remove(os.path.join(db_info['backup_root_path'], filename))

def push_to_synology(logger):
    logger.info('Connect to %s' % (SYNOLOGY_URL))
    ssh = paramiko.client.SSHClient()
    ssh.load_system_host_keys()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(SYNOLOGY_URL, username=SYNOLOGY_USERNAME, password=SYNOLOGY_PASSWORD)

    logger.info('Send backups to %s' % (SYNOLOGY_URL))

    chemin_repertoire_source = "/home/ubuntu/backups/odoo_backups/"
    chemin_repertoire_destination = os.path.join("duplicate_backups")

    try:
        # Supprimer le répertoire de destination s'il existe déjà
        commande_suppression = f'rm -rf {chemin_repertoire_destination}'
        ssh.exec_command(commande_suppression)

        # Utiliser SCP pour copier les fichiers du répertoire source vers le répertoire destination
        scp = SCPClient(ssh.get_transport())
        scp.put(chemin_repertoire_source, recursive=True, remote_path=chemin_repertoire_destination)

    finally:
        # Fermer la connexion SSH
        ssh.close()


backup_type = get_file_params()

#Let's backups

logger = create_logfile()
backup_dbs = get_db_to_backup()

push_to_synology(logger)


for backup_db in backup_dbs:
    db_info =  {
        "backup_db_url" : backup_db['name'],
        "backup_root_path" : BACKUP_PATH +"/"+ backup_db['name'] + "/"+ backup_type + "/"
    }
    make_backup(db_info, backup_type)
    remove_old_backup(db_info, backup_type)

push_to_synology(logger)
    








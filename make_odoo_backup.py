import xmlrpc.client
import requests, os
import time, datetime
import logging
import paramiko
import subprocess
from pathlib import Path
import arrow
from dotenv import load_dotenv

load_dotenv()

ODOO_URL = os.getenv('ODOO_URL')
ODOO_DB = os.getenv('ODOO_DB')
ODOO_USERNAME = os.getenv('ODOO_USERNAME')
ODOO_PASSWORD = os.getenv('ODOO_PASSWORD')
BACKUP_PATH = os.getenv('BACKUP_PATH')


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

# def push_to_synology():
# ssh = paramiko.SSHClient() 
# ssh.load_host_keys(os.path.expanduser(os.path.join("~", ".ssh", "known_hosts")))
# ssh.connect(server, username=username, password=password)
# sftp = ssh.open_sftp()
# sftp.put(localpath, remotepath)
# sftp.close()
# ssh.close()

logger = create_logfile()
backup_dbs = get_db_to_backup()

for backup_db in backup_dbs:
    backup_db_url = backup_db['name']
    db_path = BACKUP_PATH +"/"+ backup_db_url + "/last_days_backup"
    if not os.path.isdir(db_path):
        os.makedirs(db_path)

    db_path = db_path + "/"
    backup_path = db_path + '%s_%s.zip' % (backup_db_url, time.strftime('%Y_%m_%d_%H_%M_%S'))
    backup_url = 'https://'+backup_db_url+"/web/database/backup"

    subprocess.run(["curl", "-X", "POST", '-F', 'master_pwd=pPaJncYL8MgqSt', '-F', 'name=odoo', '-F', 'backup_format=zip', '-o', 
                    backup_path, backup_url],stdout=subprocess.DEVNULL,stderr=subprocess.STDOUT)
    logger.info('Let s Backup %s' % (backup_db_url))


    if subprocess.run(["curl", "-X", "POST", '-F', 'master_pwd=pPaJncYL8MgqSt', '-F', 'name=odoo' '-F', 'backup_format=zip', '-o', 
                       backup_path, backup_url],stdout=subprocess.DEVNULL,stderr=subprocess.STDOUT).returncode == 0:
        logger.info('End of %s Backup' % (backup_db_url))

    logger.info('Remove old backup of %s' % (backup_db_url))

    critical_time = arrow.now().shift(days=-1)

    for item in Path(db_path).glob('*'):
        item_time = arrow.get(item.stat().st_mtime)
        if item_time < critical_time :  
            print("Remove "+str(item.absolute()))    
            os.remove(item)
            pass
    
    logger.info('End for %s backups' % (backup_db_url))






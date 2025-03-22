# Scripts Odoo Backup

Simple script to backup my odoo instances

### Install 

First set your env variable, duplicate the .env.example file
Then install python dependency

``` pip3 install -r requirements.txt	```

### Execution
Finaly execute the backup script with a cron

``` python3 make_odoo_backup.py	```


### Cron job
You can had a cron job for automate the backup every days at 18h.

``` 00 18 * * * python3 /home/XX/backups/odoo_backup_scripts/make_odoo_backup.py ``` 


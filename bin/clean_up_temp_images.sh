#!/bin/bash

find /config/www/cctv/tmp -type f -mtime +7 -exec rm -f {} \;
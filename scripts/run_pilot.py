"""Run the self-use pilot on loopback; never expose this service publicly."""
import os
import sys
from pathlib import Path

root=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(root))
for line in (root/'.env').read_text().splitlines():
    if '=' in line and not line.lstrip().startswith('#'):
        key,value=line.split('=',1)
        os.environ.setdefault(key.strip(),value.strip().strip('"').strip("'"))
import uvicorn
from shadow_saver.pilot import create_pilot_app
if __name__=='__main__':
    uvicorn.run(create_pilot_app(),host='127.0.0.1',port=8765,access_log=False)

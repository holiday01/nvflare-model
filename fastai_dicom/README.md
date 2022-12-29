# Federated Learning with fastai_dicom

## Set nvflare enviroment
Reference page: https://nvflare.readthedocs.io/en/main/getting_started.html#running-an-example-application

```
python3 -m venv nvflare-env
source nvflare-env/bin/activate
python3 -m pip install -U pip
python3 -m pip install -U setuptools
python3 -m pip install nvflare==2.1.0

poc -n 4
```

## Start the nvflare server and client
```
./server/startup/start.sh
./site-1/startup/start.sh
./site-2/startup/start.sh
./site-3/startup/start.sh
./site-4/startup/start.sh
```

## Download and copy the fastai_dicom
```
git clone https://github.com/holiday01/nvflare-model.git
mkdir ./poc/admin/transfer
cp -r ./nvflare-model/fastai_dicom ./poc/admin/transfer
```

## Enter the nvflare admin shell
```
./poc/admin/startup/fl_admin.sh
>> submit_job fastai_dicom
```

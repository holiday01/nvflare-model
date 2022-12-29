# nvflare-model

`聯邦式學習`是一種去中心化的計算方法，強化資料的隱私性，此架構包含三種必須架構，`一、傳輸協定` `二、類神經訓練模型` `三、模型整合流程`。 <br>
在開源套件上，Nvidia提供一個架構 https://nvflare.readthedocs.io/en/main/index.html<br>
在nvidia提供的範本中，已經保有幾種的AI開發套件，如、Pytorch Tensorflow Monai等等。<br>
本團隊會加入其他Python的套件，協助快速開發聯邦式學習模型與訓練<br>


## The examples were modified cifar10 example from https://github.com/NVIDIA/NVFlare/tree/2.1/examples/cifar10
In this examples were based on Fastai.
In three exmples, there were different methods for loading data.

`timm_fastaiCXR-nvflare`
Fastai在dataloader與data transfrom，Timm架設pre-trained模型。 <br>
In this example, we used the Fastai to load and transform data, and the pre-trained model was from the timm. <br>

`fastai_mnist`
In this example, we used the Fastai to load and transform data, and the pre-trained model was from the timm. <br>
And, the example used the fastai for training the models and evaluated the global/local models. <br>
 

`fastai_dicom`
In this example, we used the Fastai to load and transform data, and the pre-trained model was from the timm. <br>
And, the example used the fastai for training the models and evaluated the global/local models.  <br>

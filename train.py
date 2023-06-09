import math
import psutil 
import mlflow
import torch
import ray
import os
import ray.util
import pytorch_lightning as pl
from filelock import FileLock
from torch.utils.data import DataLoader, random_split
from torch.nn import functional as F
from torchvision.datasets import MNIST
from torchvision import transforms
from pytorch_lightning.loggers import TensorBoardLogger
from ray import air, tune
from ray.air import session
from ray.tune import CLIReporter
from ray.tune.schedulers import ASHAScheduler, PopulationBasedTraining
from ray.tune.integration.pytorch_lightning import TuneReportCallback, TuneReportCheckpointCallback
from ray.tune.integration.mlflow import mlflow_mixin

ray.init()

class LightningMNISTClassifier(pl.LightningModule):

    def __init__(self, config, data_dir=None):
        super(LightningMNISTClassifier, self).__init__()

        self.data_dir = data_dir or os.getcwd()

        self.layer_1_size = config["layer_1_size"]
        self.layer_2_size = config["layer_2_size"]
        self.lr = config["lr"]
        self.batch_size = config["batch_size"]
        mlflow.log_param("layer_1_size", self.layer_1_size)
        mlflow.log_param("layer_2_size", self.layer_2_size)
        mlflow.log_param("lr", self.lr)
        mlflow.log_param("batch_size", self.batch_size)

        # mnist images are (1, 28, 28) (channels, width, height)
        self.layer_1 = torch.nn.Linear(28 * 28, self.layer_1_size)
        self.layer_2 = torch.nn.Linear(self.layer_1_size, self.layer_2_size)
        self.layer_3 = torch.nn.Linear(self.layer_2_size, 10)

    def forward(self, x):
        batch_size, channels, width, height = x.size()
        x = x.view(batch_size, -1)
        x = self.layer_1(x)
        x = torch.relu(x)
        x = self.layer_2(x)
        x = torch.relu(x)
        x = self.layer_3(x)
        x = torch.log_softmax(x, dim=1)
        return x

    def cross_entropy_loss(self, logits, labels):
        return F.nll_loss(logits, labels)

    def accuracy(self, logits, labels):
        _, predicted = torch.max(logits.data, 1)
        correct = (predicted == labels).sum().item()
        accuracy = correct / len(labels)
        return torch.tensor(accuracy)

    def training_step(self, train_batch, batch_idx):
        x, y = train_batch
        logits = self.forward(x)
        loss = self.cross_entropy_loss(logits, y)
        accuracy = self.accuracy(logits, y)
        self.log("train_loss", loss)
        self.log("train_accuracy", accuracy)
        return loss

    def validation_step(self, val_batch, batch_idx):
        x, y = val_batch
        logits = self.forward(x)
        loss = self.cross_entropy_loss(logits, y)
        accuracy = self.accuracy(logits, y)
        return {"val_loss": loss, "val_accuracy": accuracy}

    def validation_epoch_end(self, outputs):
        avg_loss = torch.stack([x["val_loss"] for x in outputs]).mean()
        avg_acc = torch.stack([x["val_accuracy"] for x in outputs]).mean()
        mlflow.log_metric("val_loss", avg_loss, step=self.current_epoch)
        mlflow.log_metric("val_accuracy", avg_acc, step=self.current_epoch)
        self.log("val_loss", avg_loss)
        self.log("val_accuracy", avg_acc)

    @staticmethod
    def download_data(data_dir):
        transform = transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize((0.1307, ), (0.3081, ))
        ])
        with FileLock(os.path.expanduser("~/.data.lock")):
            return MNIST(data_dir, train=True, download=True, transform=transform)

    def prepare_data(self):
        mnist_train = self.download_data(self.data_dir)
        self.mnist_train, self.mnist_val = random_split(
            mnist_train, [55000, 5000])

    def train_dataloader(self):
        return DataLoader(self.mnist_train, batch_size=int(self.batch_size), num_workers=1)

    def val_dataloader(self):
        return DataLoader(self.mnist_val, batch_size=int(self.batch_size), num_workers=1)

    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.parameters(), lr=self.lr)
        return optimizer

@mlflow_mixin
def train_mnist_tune(config, num_epochs=5, num_gpus=0, data_dir="~/data"):
    data_dir = os.path.expanduser(data_dir)
    model = LightningMNISTClassifier(config, data_dir)
    trainer = pl.Trainer(
        max_epochs=num_epochs,
        gpus=math.ceil(num_gpus),
        enable_progress_bar=False,
        callbacks=[
            TuneReportCallback(
                {
                    "loss": "val_loss",
                    "mean_accuracy": "val_accuracy"
                },
                on="validation_end")
        ])
    trainer.fit(model)
    mlflow.pytorch.log_model(model, 'model')

def tune_mnist(num_samples=4, num_epochs=5, gpus_per_trial=0, data_dir="~/data"):
    
    config = {
        "layer_1_size": tune.choice([16, 32, 64]),
        "layer_2_size": tune.choice([32, 64, 128]),
        "lr": tune.loguniform(1e-4, 1e-1),
        "batch_size": tune.choice([32, 64]),
        "mlflow": {
            "experiment_name": "ray-tuning",
            "tracking_uri": mlflow.get_tracking_uri()
        }
    }

    scheduler = ASHAScheduler(
        max_t=num_epochs,
        grace_period=1,
        reduction_factor=2)

    reporter = CLIReporter(
        parameter_columns=["layer_1_size", "layer_2_size", "lr", "batch_size"],
        metric_columns=["loss", "mean_accuracy", "training_iteration"])

    train_fn_with_parameters = tune.with_parameters(train_mnist_tune,
                                                    num_epochs=num_epochs,
                                                    num_gpus=gpus_per_trial,
                                                    data_dir=data_dir)
    resources_per_trial = {"cpu": 1, "gpu": gpus_per_trial}
    
    tuner = tune.Tuner(
        tune.with_resources(
            train_fn_with_parameters,
            resources=resources_per_trial
        ),
        tune_config=tune.TuneConfig(
            metric="loss",
            mode="min",
            scheduler=scheduler,
            num_samples=num_samples,
        ),
        run_config=air.RunConfig(
            name="tune_mnist_asha",
            progress_reporter=reporter,
        ),
        param_space=config,
    )
    results = tuner.fit()

    print("Best hyperparameters found were: ", results.get_best_result().config)

mlflow.set_experiment("ray-tuning")
tune_mnist()

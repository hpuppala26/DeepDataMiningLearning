
import argparse
import json
import logging
import math
import os
from pathlib import Path

import datasets
import evaluate
import torch
from accelerate import Accelerator
from accelerate.logging import get_logger
from accelerate.utils import set_seed
from datasets import load_dataset
from huggingface_hub import Repository, create_repo
from torch.utils.data import DataLoader
from torchvision.transforms import (
    CenterCrop,
    Compose,
    Lambda,
    Normalize,
    RandomHorizontalFlip,
    RandomResizedCrop,
    Resize,
    ToTensor,
)
from tqdm.auto import tqdm

import transformers
from transformers import AutoConfig, AutoImageProcessor, AutoModelForImageClassification, SchedulerType, get_scheduler

from DeepDataMiningLearning.hfaudio.hfutil import deviceenv_set, get_device

logger = get_logger(__name__)

def parse_args():
    parser = argparse.ArgumentParser(description="Fine-tune a Transformers model on an image classification dataset")
    parser.add_argument('--traintag', type=str, default="hfimage0214",
                    help='Name the current training')
    parser.add_argument(
        "--model_name_or_path",
        type=str,
        help="Path to pretrained model or model identifier from huggingface.co/models.",
        default="google/vit-base-patch16-224-in21k",
    )
    parser.add_argument('--usehpc', default=True, action='store_true',
                    help='Use HPC')
    parser.add_argument('--data_path', type=str, default="", help='Huggingface data cache folder') #r"D:\Cache\huggingface", "/data/cmpe249-fa23/Huggingfacecache" "/DATA10T/Cache"
    parser.add_argument('--useamp', default=True, action='store_true',
                    help='Use pytorch amp in training')
    parser.add_argument('--gpuid', default=0, type=int, help='GPU id')
    parser.add_argument('--data_name', type=str, default="beans",
                    help='data name: ')
    parser.add_argument('--dataconfig', type=str, default='en',
                    help='dataset_config_name, e.g., common_voice subset en, zh-CN')
    parser.add_argument("--train_dir", type=str, default=None, help="A folder containing the training data.")
    parser.add_argument("--validation_dir", type=str, default=None, help="A folder containing the validation data.")
    parser.add_argument(
        "--max_train_samples",
        type=int,
        default=None,
        help=(
            "For debugging purposes or quicker training, truncate the number of training examples to this "
            "value if set."
        ),
    )
    parser.add_argument(
        "--max_eval_samples",
        type=int,
        default=None,
        help=(
            "For debugging purposes or quicker training, truncate the number of evaluation examples to this "
            "value if set."
        ),
    )
    parser.add_argument(
        "--train_val_split",
        type=float,
        default=0.15,
        help="Percent to split off of train for validation",
    )
    parser.add_argument(
        "--per_device_train_batch_size",
        type=int,
        default=8,
        help="Batch size (per device) for the training dataloader.",
    )
    parser.add_argument(
        "--per_device_eval_batch_size",
        type=int,
        default=8,
        help="Batch size (per device) for the evaluation dataloader.",
    )
    parser.add_argument(
        "--learning_rate",
        type=float,
        default=5e-5,
        help="Initial learning rate (after the potential warmup period) to use.",
    )
    parser.add_argument("--weight_decay", type=float, default=0.0, help="Weight decay to use.")
    parser.add_argument("--num_train_epochs", type=int, default=3, help="Total number of training epochs to perform.")
    parser.add_argument(
        "--max_train_steps",
        type=int,
        default=None,
        help="Total number of training steps to perform. If provided, overrides num_train_epochs.",
    )
    parser.add_argument(
        "--gradient_accumulation_steps",
        type=int,
        default=1,
        help="Number of updates steps to accumulate before performing a backward/update pass.",
    )
    parser.add_argument(
        "--lr_scheduler_type",
        type=SchedulerType,
        default="linear",
        help="The scheduler type to use.",
        choices=["linear", "cosine", "cosine_with_restarts", "polynomial", "constant", "constant_with_warmup"],
    )
    parser.add_argument(
        "--num_warmup_steps", type=int, default=0, help="Number of steps for the warmup in the lr scheduler."
    )
    parser.add_argument("--output_dir", type=str, default="./output", help="Where to store the final model.")
    #parser.add_argument('--outputdir', type=str, default="/data/rnd-liu/output/", help='output path')
    parser.add_argument("--seed", type=int, default=None, help="A seed for reproducible training.")
    parser.add_argument("--push_to_hub", action="store_true", help="Whether or not to push the model to the Hub.")
    parser.add_argument(
        "--hub_model_id", type=str, help="The name of the repository to keep in sync with the local `output_dir`."
    )
    parser.add_argument("--hub_token", type=str, help="The token to use to push to the Model Hub.")
    parser.add_argument(
        "--trust_remote_code",
        type=bool,
        default=False,
        help=(
            "Whether or not to allow for custom models defined on the Hub in their own modeling files. This option "
            "should only be set to `True` for repositories you trust and in which you have read the code, as it will "
            "execute code present on the Hub on your local machine."
        ),
    )
    parser.add_argument(
        "--checkpointing_steps",
        type=str,
        default='epoch',
        help="Whether the various states should be saved at the end of every n steps, or 'epoch' for each epoch.",
    )
    parser.add_argument(
        "--saving_everynsteps",
        type=int,
        default=2,
        help="Save everying 'epoch' for each epoch.",
    )
    parser.add_argument(
        "--resume_from_checkpoint",
        type=str,
        default=None,
        help="If the training should continue from a checkpoint folder.",
    )
    parser.add_argument(
        "--with_tracking",
        action="store_true",
        help="Whether to enable experiment trackers for logging.",
    )
    parser.add_argument(
        "--report_to",
        type=str,
        default="all",
        help=(
            'The integration to report the results and logs to. Supported platforms are `"tensorboard"`,'
            ' `"wandb"`, `"comet_ml"` and `"clearml"`. Use `"all"` (default) to report to all integrations. '
            "Only applicable when `--with_tracking` is passed."
        ),
    )
    parser.add_argument(
        "--ignore_mismatched_sizes",
        action="store_true",
        help="Whether or not to enable to load a pretrained model whose head dimensions are different.",
    )
    parser.add_argument(
        "--image_column_name",
        type=str,
        default="image",
        help="The name of the dataset column containing the image data. Defaults to 'image'.",
    )
    parser.add_argument(
        "--label_column_name",
        type=str,
        default="labels",
        help="The name of the dataset column containing the labels. Defaults to 'label'.",
    )
    args = parser.parse_args()

    # Sanity checks
    if args.data_name is None and args.train_dir is None and args.validation_dir is None:
        raise ValueError("Need either a dataset name or a training/validation folder.")

    if args.push_to_hub or args.with_tracking:
        if args.output_dir is None:
            raise ValueError(
                "Need an `output_dir` to create a repo when `--push_to_hub` or `with_tracking` is specified."
            )

    if args.output_dir is not None:
        os.makedirs(args.output_dir, exist_ok=True)

    return args

def pushtohub(hub_model_id, output_dir, hub_token):
    # Retrieve of infer repo_name
    repo_name = hub_model_id
    if repo_name is None:
        repo_name = Path(output_dir).absolute().name
    # Create repo and retrieve repo_id
    repo_id = create_repo(repo_name, exist_ok=True, token=hub_token).repo_id
    # Clone repo locally
    repo = Repository(output_dir, clone_from=repo_id, token=hub_token)

    with open(os.path.join(output_dir, ".gitignore"), "w+") as gitignore:
        if "step_*" not in gitignore:
            gitignore.write("step_*\n")
        if "epoch_*" not in gitignore:
            gitignore.write("epoch_*\n")

from DeepDataMiningLearning.hfaudio.hfdata import savedict2file
def saveargs2file(args, trainoutput):
    args_dict={}
    args_str=' '
    for k, v in vars(args).items():
        args_dict[k]=v
        args_str.join(f'{k}={v}, ')
    print(args_str)
    savedict2file(data_dict=args_dict, filename=os.path.join(trainoutput,'args.json'))

import requests


def main():
    args = parse_args()
    requests.get("https://huggingface.co", timeout=5)
    #dataset = load_dataset("lhoestq/demo1")

    # Initialize the accelerator. We will let the accelerator handle device placement for us in this example.
    # If we're using tracking, we also need to initialize it here and it will by default pick up all supported trackers
    # in the environment
    accelerator_log_kwargs = {}

    if args.with_tracking:
        accelerator_log_kwargs["log_with"] = args.report_to
        accelerator_log_kwargs["project_dir"] = args.output_dir

    accelerator = Accelerator(gradient_accumulation_steps=args.gradient_accumulation_steps, **accelerator_log_kwargs)

    # Make one log on every process with the configuration for debugging.
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s", #The format of the log message: 2024/02/14 16:30:00 - INFO - main
        datefmt="%m/%d/%Y %H:%M:%S", #The format of the date and time in the log message.
        level=logging.INFO, #The minimum severity level that will be logged.
    )
    #logs the current state of the accelerator to the console
    logger.info(accelerator.state, main_process_only=False)

    if accelerator.is_main_process:
        if args.data_path:
            mycache_dir = deviceenv_set(args.usehpc, args.data_path)
        else:
            mycache_dir = '~/.cache/huggingface/'
       
        trainoutput=os.path.join(args.output_dir, args.data_name+'_'+args.traintag)
        os.makedirs(trainoutput, exist_ok=True)
        args.output_dir = trainoutput
        print("Trainoutput folder:", trainoutput)

        device, args.useamp = get_device(gpuid=args.gpuid, useamp=args.useamp)
        saveargs2file(args, trainoutput)
    
    #waits for all processes to finish before continuing
    accelerator.wait_for_everyone()

    # In distributed training, the load_dataset function guarantees that only one local process can concurrently
    # download the dataset.
    if args.data_name is not None:
        # Downloading and loading a dataset from the hub.
        dataset = load_dataset(args.data_name)
    else:
        data_files = {}
        if args.train_dir is not None:
            data_files["train"] = os.path.join(args.train_dir, "**")
        if args.validation_dir is not None:
            data_files["validation"] = os.path.join(args.validation_dir, "**")
        dataset = load_dataset(
            "imagefolder",
            data_files=data_files,
        )
        # See more about loading custom images at
        # https://huggingface.co/docs/datasets/v2.0.0/en/image_process#imagefolder.

    dataset_column_names = dataset["train"].column_names if "train" in dataset else dataset["validation"].column_names
    #'image': PIL image object, 'labels': int, 'image_file_path': path
    if args.image_column_name not in dataset_column_names:
        raise ValueError(
            f"--image_column_name {args.image_column_name} not found in dataset '{args.data_name}'. "
            "Make sure to set `--image_column_name` to the correct audio column - one of "
            f"{', '.join(dataset_column_names)}."
        )
    if args.label_column_name not in dataset_column_names:
        raise ValueError(
            f"--label_column_name {args.label_column_name} not found in dataset '{args.data_name}'. "
            "Make sure to set `--label_column_name` to the correct text column - one of "
            f"{', '.join(dataset_column_names)}."
        )

    # If we don't have a validation split, split off a percentage of train as validation.
    args.train_val_split = None if "validation" in dataset.keys() else args.train_val_split
    if isinstance(args.train_val_split, float) and args.train_val_split > 0.0:
        split = dataset["train"].train_test_split(args.train_val_split)
        dataset["train"] = split["train"]
        dataset["validation"] = split["test"]

    # Prepare label mappings.
    # We'll include these in the model's config to get human readable labels in the Inference API.
    #By default the ClassLabel fields are encoded into integers
    classlabel = dataset["train"].features[args.label_column_name] #ClassLabel(num_classes=x, names=[''], id=None)
    labels = classlabel.names
    label2id = {label: str(i) for i, label in enumerate(labels)}
    id2label = {str(i): label for i, label in enumerate(labels)}

    #add testing code to fetch one sample data from dataset and print the shape of data
    # {
    #     "image": "train/cat/00000.png",
    #     "label": 0
    # }
    print(dataset["train"][0])
    print(dataset["train"][0][args.label_column_name])
    #The Datasets library is made for processing data very easily. We can write custom functions, 
    #which can then be applied on an entire dataset (either using .map() or .set_transform()).


    # Load pretrained model and image processor
    #
    # In distributed training, the .from_pretrained methods guarantee that only one local process can concurrently
    # download model & vocab.
    config = AutoConfig.from_pretrained(
        args.model_name_or_path,
        num_labels=len(labels),
        i2label=id2label,
        label2id=label2id,
        #cache_dir=mycache_dir,  
        finetuning_task="image-classification",
        trust_remote_code=args.trust_remote_code,
    )
    image_processor = AutoImageProcessor.from_pretrained(
        args.model_name_or_path,
        #cache_dir=mycache_dir,
        trust_remote_code=args.trust_remote_code,
    )
    model = AutoModelForImageClassification.from_pretrained(
        args.model_name_or_path,
        config=config,
        #cache_dir=mycache_dir,
        ignore_mismatched_sizes=args.ignore_mismatched_sizes,
        trust_remote_code=args.trust_remote_code,
    )

    # Preprocessing the datasets
    # Define torchvision transforms to be applied to each image.
    if "shortest_edge" in image_processor.size:
        size = image_processor.size["shortest_edge"]
    else:
        size = (image_processor.size["height"], image_processor.size["width"]) #(224, 224)
    
    normalize = (
        Normalize(mean=image_processor.image_mean, std=image_processor.image_std)
        if hasattr(image_processor, "image_mean") and hasattr(image_processor, "image_std")
        else Lambda(lambda x: x)
    )
    train_transforms = Compose(
        [
            RandomResizedCrop(size),
            RandomHorizontalFlip(),
            ToTensor(),
            normalize,
        ]
    )
    val_transforms = Compose(
        [
            Resize(size),
            CenterCrop(size),
            ToTensor(),
            normalize,
        ]
    )

    def preprocess_train(example_batch):
        """Apply _train_transforms across a batch."""
        example_batch["pixel_values"] = [
            train_transforms(image.convert("RGB")) for image in example_batch[args.image_column_name]
        ]
        return example_batch

    def preprocess_val(example_batch):
        """Apply _val_transforms across a batch."""
        example_batch["pixel_values"] = [
            val_transforms(image.convert("RGB")) for image in example_batch[args.image_column_name]
        ]
        return example_batch

    with accelerator.main_process_first():
        if args.max_train_samples is not None:
            dataset["train"] = dataset["train"].shuffle(seed=args.seed).select(range(args.max_train_samples))
        # Set the training transforms
        train_dataset = dataset["train"].with_transform(preprocess_train)
        if args.max_eval_samples is not None:
            dataset["validation"] = dataset["validation"].shuffle(seed=args.seed).select(range(args.max_eval_samples))
        # Set the validation transforms
        eval_dataset = dataset["validation"].with_transform(preprocess_val)

    # DataLoaders creation:
    #used to batch examples together. Each batch consists of 2 keys, namely pixel_values and labels.
    def collate_fn(examples):
        pixel_values = torch.stack([example["pixel_values"] for example in examples])
        labels = torch.tensor([example[args.label_column_name] for example in examples])
        return {"pixel_values": pixel_values, "labels": labels}

    train_dataloader = DataLoader(
        train_dataset, shuffle=True, collate_fn=collate_fn, batch_size=args.per_device_train_batch_size
    )
    eval_dataloader = DataLoader(eval_dataset, collate_fn=collate_fn, batch_size=args.per_device_eval_batch_size)

    # Optimizer
    # Split weights in two groups, one with weight decay and the other not.
    no_decay = ["bias", "LayerNorm.weight"]
    optimizer_grouped_parameters = [
        {
            "params": [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
            "weight_decay": args.weight_decay,
        },
        {
            "params": [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)],
            "weight_decay": 0.0,
        },
    ]
    optimizer = torch.optim.AdamW(optimizer_grouped_parameters, lr=args.learning_rate)

    # Scheduler and math around the number of training steps.
    overrode_max_train_steps = False
    num_update_steps_per_epoch = math.ceil(len(train_dataloader) / args.gradient_accumulation_steps)
    if args.max_train_steps is None:
        args.max_train_steps = args.num_train_epochs * num_update_steps_per_epoch
        overrode_max_train_steps = True

    lr_scheduler = get_scheduler(
        name=args.lr_scheduler_type,
        optimizer=optimizer,
        num_warmup_steps=args.num_warmup_steps * accelerator.num_processes,
        num_training_steps=args.max_train_steps
        if overrode_max_train_steps
        else args.max_train_steps * accelerator.num_processes,
    )

    # Prepare everything with our `accelerator`.
    model, optimizer, train_dataloader, eval_dataloader, lr_scheduler = accelerator.prepare(
        model, optimizer, train_dataloader, eval_dataloader, lr_scheduler
    )

    # We need to recalculate our total training steps as the size of the training dataloader may have changed.
    num_update_steps_per_epoch = math.ceil(len(train_dataloader) / args.gradient_accumulation_steps)
    if overrode_max_train_steps:
        args.max_train_steps = args.num_train_epochs * num_update_steps_per_epoch
    # Afterwards we recalculate our number of training epochs
    args.num_train_epochs = math.ceil(args.max_train_steps / num_update_steps_per_epoch)

    # Figure out how many steps we should save the Accelerator states
    checkpointing_steps = args.checkpointing_steps
    if checkpointing_steps is not None and checkpointing_steps.isdigit():
        checkpointing_steps = int(checkpointing_steps)

    # We need to initialize the trackers we use, and also store our configuration.
    # The trackers initializes automatically on the main process.
    if args.with_tracking:
        experiment_config = vars(args)
        # TensorBoard cannot log Enums, need the raw value
        experiment_config["lr_scheduler_type"] = experiment_config["lr_scheduler_type"].value
        accelerator.init_trackers("image_classification_no_trainer", experiment_config)

    # Get the metric function
    metric = evaluate.load("accuracy")

    # Train!
    total_batch_size = args.per_device_train_batch_size * accelerator.num_processes * args.gradient_accumulation_steps

    logger.info("***** Running training *****")
    logger.info(f"  Num examples = {len(train_dataset)}")
    logger.info(f"  Num Epochs = {args.num_train_epochs}")
    logger.info(f"  Instantaneous batch size per device = {args.per_device_train_batch_size}")
    logger.info(f"  Total train batch size (w. parallel, distributed & accumulation) = {total_batch_size}")
    logger.info(f"  Gradient Accumulation steps = {args.gradient_accumulation_steps}")
    logger.info(f"  Total optimization steps = {args.max_train_steps}")
    # Only show the progress bar once on each machine.
    progress_bar = tqdm(range(args.max_train_steps), disable=not accelerator.is_local_main_process)
    completed_steps = 0
    starting_epoch = 0
    # Potentially load in the weights and states from a previous save
    if args.resume_from_checkpoint:
        if args.resume_from_checkpoint is not None or args.resume_from_checkpoint != "":
            checkpoint_path = args.resume_from_checkpoint
            path = os.path.basename(args.resume_from_checkpoint)
        else:
            # Get the most recent checkpoint
            dirs = [f.name for f in os.scandir(os.getcwd()) if f.is_dir()]
            dirs.sort(key=os.path.getctime)
            path = dirs[-1]  # Sorts folders by date modified, most recent checkpoint is the last
            checkpoint_path = path
            path = os.path.basename(checkpoint_path)

        accelerator.print(f"Resumed from checkpoint: {checkpoint_path}")
        accelerator.load_state(checkpoint_path)
        # Extract `epoch_{i}` or `step_{i}`
        training_difference = os.path.splitext(path)[0]

        if "epoch" in training_difference:
            starting_epoch = int(training_difference.replace("epoch_", "")) + 1
            resume_step = None
            completed_steps = starting_epoch * num_update_steps_per_epoch
        else:
            # need to multiply `gradient_accumulation_steps` to reflect real steps
            resume_step = int(training_difference.replace("step_", "")) * args.gradient_accumulation_steps
            starting_epoch = resume_step // len(train_dataloader)
            completed_steps = resume_step // args.gradient_accumulation_steps
            resume_step -= starting_epoch * len(train_dataloader)

    # update the progress_bar if load from checkpoint
    progress_bar.update(completed_steps)

    for epoch in range(starting_epoch, args.num_train_epochs):
        model.train()
        if args.with_tracking:
            total_loss = 0
        if args.resume_from_checkpoint and epoch == starting_epoch and resume_step is not None:
            # We skip the first `n` batches in the dataloader when resuming from a checkpoint
            active_dataloader = accelerator.skip_first_batches(train_dataloader, resume_step)
        else:
            active_dataloader = train_dataloader
        for step, batch in enumerate(active_dataloader):
            with accelerator.accumulate(model):
                outputs = model(**batch)
                loss = outputs.loss
                # We keep track of the loss at each epoch
                if args.with_tracking:
                    total_loss += loss.detach().float()
                accelerator.backward(loss)
                optimizer.step()
                lr_scheduler.step()
                optimizer.zero_grad()

            # Checks if the accelerator has performed an optimization step behind the scenes
            if accelerator.sync_gradients:
                progress_bar.update(1)
                completed_steps += 1

            if completed_steps >= args.max_train_steps:
                break

        model.eval()
        for step, batch in enumerate(eval_dataloader):
            with torch.no_grad():
                outputs = model(**batch)
            predictions = outputs.logits.argmax(dim=-1)
            predictions, references = accelerator.gather_for_metrics((predictions, batch["labels"]))
            metric.add_batch(
                predictions=predictions,
                references=references,
            )

        eval_metric = metric.compute()
        logger.info(f"epoch {epoch}: {eval_metric}")

        if args.with_tracking:
            accelerator.log(
                {
                    "accuracy": eval_metric,
                    "train_loss": total_loss.item() / len(train_dataloader),
                    "epoch": epoch,
                    "step": completed_steps,
                },
                step=completed_steps,
            )

        #saving the models
        if args.checkpointing_steps == "epoch" and epoch % args.saving_everynsteps ==0:
            accelerator.wait_for_everyone()
            unwrapped_model = accelerator.unwrap_model(model)
            output_dir = f"epoch_{epoch}"
            if args.output_dir is not None:
                output_dir = os.path.join(args.output_dir, output_dir)
            accelerator.save_state(output_dir)
            unwrapped_model.save_pretrained(
                output_dir, is_main_process=accelerator.is_main_process, save_function=accelerator.save
            )
            if accelerator.is_main_process:
                image_processor.save_pretrained(output_dir)

    if args.with_tracking:
        accelerator.end_training()

    if args.output_dir is not None:
        accelerator.wait_for_everyone()
        unwrapped_model = accelerator.unwrap_model(model)
        unwrapped_model.save_pretrained(
            args.output_dir, is_main_process=accelerator.is_main_process, save_function=accelerator.save
        )
        if accelerator.is_main_process:
            image_processor.save_pretrained(args.output_dir)
            # if args.push_to_hub:
            #     repo.push_to_hub(commit_message="End of training", auto_lfs_prune=True)

            all_results = {f"eval_{k}": v for k, v in eval_metric.items()}
            with open(os.path.join(args.output_dir, "all_results.json"), "w") as f:
                json.dump(all_results, f)

from PIL import Image
import requests
def inference():
    url = 'https://huggingface.co/nielsr/convnext-tiny-finetuned-eurostat/resolve/main/forest.png'
    image = Image.open(requests.get(url, stream=True).raw)
    #show the PIL image
    image.show()
    #load the model
    model = AutoModelForImageClassification.from_pretrained("nielsr/convnext-tiny-finetuned-eurostat")
    #load the image processor
    processor = AutoImageProcessor.from_pretrained("nielsr/convnext-tiny-finetuned-eurostat")
    #preprocess the image
    inputs = processor(image.convert("RGB"), return_tensors="pt")
    print(inputs.pixel_values.shape) #torch.Size([1, 3, 224, 224])
    #inference
    with torch.no_grad():
        outputs = model(**inputs)
    #get the prediction
    logits = outputs.logits #torch.Size([1, 10])
    predictions = logits.argmax(dim=-1) #torch.Size([1])
    #print the prediction
    print(predictions)
    predicted_class_idx = predictions[0].item() #1
    print("Predicted class:", model.config.id2label[predicted_class_idx])

if __name__ == "__main__":
    inference()
    main()
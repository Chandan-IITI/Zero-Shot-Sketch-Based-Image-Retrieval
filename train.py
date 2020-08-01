# TODO: write metrics to wandb, save checkpoint, best metric - only test(as triplets), save images for attention viz


import time
import datetime
import pytz 

import numpy as np
import torch 
import torch.nn as nn
import torch.nn.functional as F
import torchvision.utils as vutils 
from torch.utils.tensorboard import SummaryWriter
import wandb


from model.net import MainModel
from model.loss import DetangledJointDomainLoss
from model.dataloader import Dataloaders
from evaluate import evaluate
from utils import *

class Trainer():
  def __init__(self, data_dir, dataloaders):
    self.dataloaders = dataloaders # temporary, to save time
    self.train_dict = self.dataloaders.train_dict
    self.test_dict = self.dataloaders.test_dict
  
  def train_and_evaluate(self, config, checkpoint_file, local):

    batch_size = config['batch_size']
    device = torch.device('cuda') if torch.cuda.is_available() else torch.device('cpu')

    train_dataloader, train_dict = self.dataloaders.get_train_dataloader(batch_size = batch_size) 
    num_batches = len(train_dataloader) 

    image_model = MainModel(pretrained = config['pretrained'], output_embedding_size = config['embedding_size'], use_attention = config['use_attention'])
    sketch_model = MainModel(pretrained = config['pretrained'], output_embedding_size = config['embedding_size'], use_attention = config['use_attention'])
    loss_model = DetangledJointDomainLoss(input_size = config['embedding_size'], grl_lambda = config['grl_lambda'], w_dom = config['w_dom'], w_triplet = config['w_triplet'], w_sem = config['w_sem'])

    image_model = image_model.to(device); sketch_model = sketch_model.to(device); loss_model = loss_model.to(device);

    params = [param for param in image_model.parameters() if param.requires_grad == True]
    params.extend([param for param in sketch_model.parameters() if param.requires_grad == True])   
    params.extend([param for param in loss_model.parameters() if param.requires_grad == True])
    print('A total of %d parameters in present model' % (len(params)))
    #TODO: try different optimizers for different models

    optimizer = torch.optim.Adam(params, lr=config['lr'])

    lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size = config['lr_scheduler_step_size'], gamma = 0.1)
    for i in range(config['start_epoch']):
      lr_scheduler.step() 
    print('Training...')    
    return
    for epoch in range(config['start_epoch'], config['epochs']):
      accumulated_loss_total = RunningAverage()
      accumulated_loss_dom = RunningAverage()
      accumulated_loss_sem = RunningAverage()
      accumulated_loss_triplet = RunningAverage()
      accumulated_iteration_time = RunningAverage()

      epoch_start_time = time.time()

      image_model.train(); sketch_model.train(); loss_model.train()

      for iteration, batch in enumerate(train_dataloader):
        # wandb_step += 1

        time_start = time.time()        

        optimizer.zero_grad()

        '''GETTING THE DATA'''
        triplets, label_embeddings, positive_label_idxs, negative_label_idxs = batch
        anchors = triplets['anchor']; positives = triplets['positive']; negatives = triplets['negative'];
        anchors = torch.autograd.Variable(anchors.to(device)); positives = torch.autograd.Variable(positives.to(device))
        negatives = torch.autograd.Variable(negatives.to(device)); label_embeddings = torch.autograd.Variable(label_embeddings.to(device))

        '''INFERENCE AND LOSS'''
        pred_sketch_features = sketch_model(anchors)
        pred_positives_features = image_model(positives)
        pred_negatives_features = image_model(negatives)
        total_loss, loss_domain, loss_triplet, loss_semantic = loss_model(pred_sketch_features, pred_positives_features,
                                                                          pred_negatives_features, label_embeddings,
                                                                          epoch) # epoch is sent to anneal/temper domain GRL lambda
        accumulated_loss_total.update(total_loss, batch_size)
        accumulated_loss_dom.update(loss_domain, batch_size)
        accumulated_loss_sem.update(loss_semantic, batch_size)
        accumulated_loss_triplet.update(loss_triplet, batch_size)
        
        '''OPTIMIZATION'''
        total_loss.backward()
        optimizer.step()

        '''TIME UTILS & PRINTING'''
        time_end = time.time()
        accumulated_iteration_time.update(time_end - time_start)
        eta_cur_epoch = str(datetime.timedelta(seconds = int(accumulated_iteration_time() * (num_batches - iteration))))

        if iteration % config['print_every'] == 0:
          print(datetime.datetime.now(pytz.timezone('Asia/Kolkata')), end = ' ')
          print('Epoch: %d [%d / %d] ; eta: %s' % (epoch, iteration, num_batches, eta))
          print('Total loss: %f(%f); Domain loss: %f(%f); Semantic loss: %f(%f); Triplet loss: %f(%f)' % \
          (total_loss, accumulated_loss_total(), loss_domain, accumulated_loss_dom(), loss_semantic, accumulated_loss_sem(), loss_triplet, accumulated_loss_triplet()))

      '''END OF EPOCH'''
      epoch_end_time = time.time()
      print('Epoch %d complete, time taken: %s' % (epoch, str(datetime.timedelta(seconds = int(epoch_end_time - epoch_start_time)))))
      lr_scheduler.step()
      del triplets; del label_embeddings; del pred_sketch_features; del pred_positives_features; del pred_negatives_features
      torch.cuda.empty_cache()


      '''EVALUATE WITH THE TEST SET'''

      test_mAP = evaluate(dataloaders.get_test_dataloader(batch_size = batch_size/2, section = 'images'), dataloaders.get_test_dataloader(batch_size = batch_size/2, section = 'sketches'), image_model, sketch_model)
      print('Test mAP: %f' % (test_mAP))
# -*- coding: utf-8 -*-
'''
Author: Ioannis Kakogeorgiou
Email: gkakogeorgiou@gmail.com
Python Version: 3.7.10
Description: evaluation.py includes the code in order to produce
             the evaluation for each class as well as the prediction
             masks for the pixel-level semantic segmentation.
'''

import os
import sys
import random
import logging
import rasterio
import argparse
import numpy as np
from tqdm import tqdm
from os.path import dirname as up

import torch
from torch.utils.data import DataLoader
import torchvision.transforms as transforms

sys.path.append(up(os.path.abspath(__file__)))

from DATA_G1_19 import GenDEBRIS, bands_mean, bands_std

sys.path.append(os.path.join(up(up(up(os.path.abspath(__file__)))), 'utils'))
from metrics import Evaluation, confusion_matrix
from ASSETS_G1 import labels

random.seed(0)
np.random.seed(0)
torch.manual_seed(0)

root_path = up(up(up(os.path.abspath(__file__))))

logging.basicConfig(filename=os.path.join(root_path, 'logs','evaluating_unet.log'), filemode='a',level=logging.INFO, format='%(name)s - %(levelname)s - %(message)s')
logging.info('*'*10)

import segmentation_models_pytorch as smp

def main(options):
    # Transformations
    
    transform_test = transforms.Compose([transforms.ToTensor()])
    standardization = transforms.Normalize(bands_mean, bands_std)
    
    # Construct Data loader

    dataset_test = GenDEBRIS('test', transform=transform_test, standardization = standardization, agg_to_water = options['agg_to_water'])

    test_loader = DataLoader(   dataset_test, 
                                batch_size = options['batch'], 
                                shuffle = False)
    


    # Use gpu or cpu
    if torch.cuda.is_available():
        device = torch.device("cuda")
    else:
        device = torch.device("cpu")
        
    model = smp.Unet(
    encoder_name="vgg16",        # choose encoder, e.g. mobilenet_v2 or efficientnet-b7
    encoder_weights="imagenet",     # use `imagenet` pre-trained weights for encoder initialization
    in_channels=19,                  # model input channels (1 for gray-scale images, 3 for RGB, etc.)
    classes=5,                      # model output channels (number of classes in your dataset)
    )

    model.to(device)

    # Load model from specific epoch to continue the training or start the evaluation
    model_file = options['model_path']
    logging.info('Loading model files from folder: %s' % model_file)

    checkpoint = torch.load(model_file, map_location = device)
    model.load_state_dict(checkpoint)

    del checkpoint  # dereference
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    model.eval()

    y_true = []
    y_predicted = []
    
    with torch.no_grad():
        for (image, target) in tqdm(test_loader, desc="testing"):

            image = image.to(device)
            #print(image.shape)
            target = target.to(device)

            logits = model(image)

            # Accuracy metrics only on annotated pixels
            logits = torch.movedim(logits, (0,1,2,3), (0,3,1,2))
            logits = logits.reshape((-1,options['output_channels']))
            target = target.reshape(-1)
            mask = target != -1
            logits = logits[mask]
            target = target[mask]
            
            probs = torch.nn.functional.softmax(logits, dim=1).cpu().numpy()
            target = target.cpu().numpy()
            
            y_predicted += probs.argmax(1).tolist()
            y_true += target.tolist()
        
        ####################################################################
        # Save Scores to the .log file                                     #
        ####################################################################
        acc = Evaluation(y_predicted, y_true)
        logging.info("\n")
        logging.info("STATISTICS: \n")
        logging.info("Evaluation: " + str(acc))
        print("Evaluation: " + str(acc))
        conf_mat = confusion_matrix(y_true, y_predicted, labels)
        logging.info("Confusion Matrix:  \n" + str(conf_mat.to_string()))
        print("Confusion Matrix:  \n" + str(conf_mat.to_string()))
        
        if options['predict_masks']:
            
            path = os.path.join(root_path, 'data', 'patches')
            indices_path = os.path.join(root_path, 'data', 'indices')
            textures_path = os.path.join(root_path, 'data', 'texture')
            ROIs = np.genfromtxt(os.path.join(root_path, 'data', 'splits', 'test_X.txt'),dtype='str')

            impute_nan = np.tile(bands_mean, (256,256,1))
                        
            for roi in tqdm(ROIs):
            
                roi_folder = '_'.join(['S2'] + roi.split('_')[:-1])             # Get Folder Name
                roi_name = '_'.join(['S2'] + roi.split('_'))                    # Get File Name
                roi_file = os.path.join(path, roi_folder,roi_name + '.tif')     # Get File path
                roi_indices_file = os.path.join(indices_path, roi_folder, roi_name + '_si.tif')
                roi_texture_file = os.path.join(textures_path, roi_folder, roi_name + '_glcm.tif')

                os.makedirs(options['gen_masks_path'], exist_ok=True)
            
                output_image = os.path.join(options['gen_masks_path'], os.path.basename(roi_file).split('.tif')[0] + '_unet.tif')
            
                # Read metadata of the initial image
                with rasterio.open(roi_file, mode ='r') as src:
                    tags = src.tags().copy()
                    meta = src.meta
                    image_patch = src.read()
                    image_patch = np.moveaxis(image_patch, (0, 1, 2), (2, 0, 1))
                   
                    dtype = src.read(1).dtype

                # Read metadata of indices image
                with rasterio.open(roi_indices_file, mode ='r') as src:
                    tags = src.tags().copy()
                    meta = src.meta
                    image_indices = src.read()
                    image_indices = np.moveaxis(image_indices, (0, 1, 2), (2, 0, 1))
                    
                    dtype = src.read(1).dtype

                # Read metadata of textures image
                with rasterio.open(roi_texture_file, mode ='r') as src:
                    tags = src.tags().copy()
                    meta = src.meta
                    image_t = src.read()
                    image_t = np.moveaxis(image_t, (0, 1, 2), (2, 0, 1))
                    
                    dtype = src.read(1).dtype
                
                stacked_input = np.concatenate((image_patch,image_indices), axis = 2)
            
                # Update meta to reflect the number of layers
                meta.update(count = 1)
            
                # Write it
                with rasterio.open(output_image, 'w', **meta) as dst:
                    
                    # Preprocessing before prediction
                    nan_mask = np.isnan(stacked_input)
                    
                    stacked_input[nan_mask] = impute_nan[nan_mask]
            
                    stacked_input = transform_test(stacked_input)
                    
                    stacked_input = standardization(stacked_input)
                    
                    # Image to Cuda if exist
                    stacked_input = stacked_input.to(device)
            
                    # Predictions
                    logits = model(stacked_input.unsqueeze(0))
            
                    probs = torch.nn.functional.softmax(logits.detach(), dim=1).cpu().numpy()
            
                    probs = probs.argmax(1).squeeze()+1
                    
                    # Write the mask with georeference
                    dst.write_band(1, probs.astype(dtype).copy()) # In order to be in the same dtype
                    dst.update_tags(**tags)

if __name__ == "__main__":

    parser = argparse.ArgumentParser()

    # Options
    parser.add_argument('--agg_to_water', default=True, type=bool,  help='Aggregate Mixed Water, Wakes, Cloud Shadows, Waves with Marine Water')
    
    parser.add_argument('--batch', default=5, type=int, help='Number of epochs to run')
    
    # Unet parameters
    parser.add_argument('--input_channels', default=19, type=int, help='Number of input bands')
    parser.add_argument('--output_channels', default=5, type=int, help='Number of output classes')
    parser.add_argument('--hidden_channels', default=16, type=int, help='Number of hidden features')
    
    # Unet model path
    parser.add_argument('--model_path', default=os.path.join(up(os.path.abspath(__file__)), 'trained_models', '44', 'model.pth'), help='Path to Unet pytorch model')
    
    # Produce Predicted Masks
    parser.add_argument('--predict_masks', default= True, type=bool, help='Generate test set prediction masks?')
    parser.add_argument('--gen_masks_path', default=os.path.join(root_path, 'data', 'predicted_unet'), help='Path to where to produce store predictions')

    args = parser.parse_args()
    options = vars(args)  # convert to ordinary dict
    
    main(options)
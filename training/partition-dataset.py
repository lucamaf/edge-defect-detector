import os 
import random
import shutil
from sklearn.model_selection import train_test_split


random.seed(108)

# Read images and annotations
images = [os.path.join('dataset/images', x) for x in os.listdir('dataset/images')]
annotations = [os.path.join('dataset/labels', x) for x in os.listdir('dataset/labels') if x[-3:] == "txt"]

images.sort()
annotations.sort()

# Split the dataset into train-valid-test splits 
train_images, val_images, train_annotations, val_annotations = train_test_split(images, annotations, test_size = 0.2, random_state = 1)
val_images, test_images, val_annotations, test_annotations = train_test_split(val_images, val_annotations, test_size = 0.5, random_state = 1)

#Utility function to move images 
def move_files_to_folder(list_of_files, destination_folder):
    for f in list_of_files:
        try:
            shutil.move(f, destination_folder)
        except:
            print(f)
            assert False

# Move the splits into their folders
move_files_to_folder(train_images, 'partition/train')
move_files_to_folder(val_images, 'partition/val')
move_files_to_folder(test_images, 'partition/test')
move_files_to_folder(train_annotations, 'partition/train')
move_files_to_folder(val_annotations, 'partition/val')
move_files_to_folder(test_annotations, 'partition/test')

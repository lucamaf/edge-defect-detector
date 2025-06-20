# import packages to retrieve and display image files
import os
import shutil

# store working directory path as work_dir
work_dir = os.getcwd()

# print work_dir path
print(os.getcwd())

# print work_dir contents
print(os.listdir(f"{work_dir}"))


# function to reorganize dir
def organize_files(directory):
    for subdir in ['train', 'test', 'val']:
        subdir_path = os.path.join(directory, subdir)
        if not os.path.exists(subdir_path):
            continue

        images_dir = os.path.join(subdir_path, 'images')
        labels_dir = os.path.join(subdir_path, 'labels')

        # create image and label subdirs if non-existent
        os.makedirs(images_dir, exist_ok=True)
        os.makedirs(labels_dir, exist_ok=True)

        # move images and labels to respective subdirs
        for filename in os.listdir(subdir_path):
            if filename.endswith('.txt'):
                shutil.move(os.path.join(subdir_path, filename), os.path.join(labels_dir, filename))
            elif filename.endswith('.jpg') or filename.endswith('.png') or filename.endswith('.jpeg'):
                shutil.move(os.path.join(subdir_path, filename), os.path.join(images_dir, filename))
            # delete .xml files
            elif filename.endswith('.xml'):
                os.remove(os.path.join(subdir_path, filename))

if __name__ == "__main__":
    directory = f"{work_dir}/partition"
    organize_files(directory)


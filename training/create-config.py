import ultralytics
ultralytics.checks()
from ultralytics import YOLO
import yaml

# contents of new confg.yaml file
def update_yaml_file(file_path):
    data = {
        'path': '/home/luca/YOLOv8/partition',
        'train': 'train/images',
        'val': 'train/images',
        'test': 'test/images',
        'names': {
            0: 'Airplane',
            1: 'Car',
            2: 'Red-Hat',
            3: 'Scratch'
        }
    }

    # ensures the "names" list appears after the sub/directories
    names_data = data.pop('names')
    with open(file_path, 'w') as yaml_file:
        yaml.dump(data, yaml_file)
        yaml_file.write('\n')
        yaml.dump({'names': names_data}, yaml_file) 

if __name__ == "__main__":
    file_path = f"/home/luca/YOLOv8/config.yaml" #.yaml file path
    update_yaml_file(file_path)
    print(f"{file_path} updated successfully.")

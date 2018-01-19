#!/bin/sh

set -ex

DIR=$PWD
INITRD_SRC=/home/darrenwu/workspace/projects/caracalla/ubuntu-server/18.04/image/live-subiquity/initrd
YAML=/home/darrenwu/workspace/projects/caracalla/ubuntu-server/18.04/subiquity/examples/answers-partitions.yaml

#update initrd
rm new_iso/casper/initrd.lz
cd $INITRD_SRC
find | cpio --quiet -o -H newc | xz -c9 --check=crc32 > $DIR/new_iso/casper/initrd.lz
cd $DIR

#enable console
sed -i 's@quiet splash@console=tty0 console=ttyS0,115200n8@g' new_iso/boot/grub/grub.cfg

#add answer.yaml
mkdir new_installer/subiquity_config/
cp $YAML new_installer/subiquity_config/answers.yaml

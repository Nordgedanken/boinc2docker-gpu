#!/usr/bin/env python

import argparse
import boinc_path_config
import json
import sys
import os
import tarfile
import xml.etree.cElementTree as ET
from Boinc.create_work import add_create_work_args, read_create_work_args, create_work, projdir, dir_hier_path
from functools import partial
from os.path import join, split, exists, basename
from subprocess import check_output
from xml.dom import minidom
from inspect import currentframe
from textwrap import dedent
from uuid import uuid4 as uuid
from tempfile import mkdtemp


def boinc2docker_create_work(image,
                             command=None,
                             input_files=None,
                             appname='boinc2docker',
                             entrypoint=None,
                             prerun=None,
                             postrun=None,
                             verbose=True,
                             create_work_args=None):
    """

    Arguments:
        image - name of Docker image
        command - command (if any) to run as either string or list arguments
                  e.g ['echo','foo'] or 'echo foo'
        input_files - list of (open_name,contents,flags) for any extra files for this job
                      e.g. [('shared/foo','bar',['gzip','nodelete'])]
        appname - appname for which to submit job
        entrypoint - override default entrypoint
        prerun/postrun - command to run in the boinc_app script before/after the docker run
        verbose - print extra info
        create_work_args - any extra arguments to pass to the job, e.g. {'target_nresults':1}
    """

    fmt = partial(lambda s,f: s.format(**dict(globals(),**f.f_locals)),f=currentframe())
    sh = lambda cmd: check_output(['sh','-c',fmt(cmd)]).strip()
    tmpdir = mkdtemp()

    if prerun is None: prerun=""
    if postrun is None: postrun=""
    if command is None: command=""
    if create_work_args is None: create_work_args=dict()
    if ':' not in image: image+=':latest'

    try:

        #get entire image as a tar file
        image_id = sh('docker inspect --format "{{{{ .Id }}}}" {image}').strip().split(':')[1]
        image_filename = fmt("image_{image_id}.tar")
        image_path = dir_hier_path(image_filename)

        if exists(image_path):
            if verbose: print fmt("Image already imported into BOINC. Reading existing info...")
            need_extract = False
            manifest = json.load(tarfile.open(image_path).extractfile('manifest.json'))
        else:
            if verbose: print fmt("Exporting image '{image}' to tar file...")
            need_extract = True
            sh("docker save {image} | tar xf - -C {tmpdir}")
            manifest = json.load(open(join(tmpdir,'manifest.json')))


        #start with any extra custom input files
        if input_files is None: input_files=[]
        else: 
            input_files = [(open_name,(basename(open_name),contents),flags) 
                           for open_name,contents,flags in input_files]

        #generate boinc_app script
        if isinstance(command,str): command=[command.split()]
        command = ' '.join('"'+str(x)+'"' for x in command)
        entrypoint = '--entrypoint '+entrypoint if entrypoint else ''
        script = fmt(dedent("""
        #!/bin/sh
        set -e 

        echo "Importing Docker data from BOINC..."
        mkdir -p /tmp/image
        cat /root/shared/image/*.tar | tar xi -C /tmp/image
        tar cf - -C /tmp/image . | docker load 
        rm -rf /tmp/image

        echo "Prerun diagnostics..."
        docker images
        docker ps -a
        du -sh /var/lib/docker
        free -m

        echo "Prerun commands..."
        {prerun}

        echo "Running... "
        docker run --rm -v /root/shared:/root/shared {entrypoint} {image} {command}

        echo "Postrun commands..."
        {postrun}
        """))
        input_files.append(('shared/boinc_app',('boinc_app',script),[]))

        layer_flags = ['sticky','no_delete','gzip']

        #extract layers to individual tar files, directly into download dir
        for layer in manifest[0]['Layers']:
            layer_id = split(layer)[0]
            layer_filename = fmt("layer_{layer_id}.tar")
            layer_path = sh("bin/dir_hier_path {layer_filename}")
            input_files.append((fmt("shared/image/{layer_filename}"),layer_filename,layer_flags))
            if need_extract and not exists(layer_path): 
                if verbose: print fmt("Creating input file for layer %s..."%layer_id[:12])
                sh("tar cvf {layer_path} -C {tmpdir} {layer_id}")
                sh("gzip -k {layer_path}")


        #extract remaining image info to individual tar file, directly into download dir
        input_files.append((fmt("shared/image/{image_filename}"),image_filename,layer_flags))
        if need_extract: 
            if verbose: print fmt("Creating input file for image %s..."%image_id[:12])
            sh("tar cvf {image_path} -C {tmpdir} {image_id}.json manifest.json repositories")
            sh("gzip -k {image_path}")

        #generate input template
        if verbose: print fmt("Creating input template for job...")
        root = ET.Element("input_template")
        workunit = ET.SubElement(root, "workunit")
        for i,(open_name,_,flags) in enumerate(input_files):
            fileinfo = ET.SubElement(root, "file_info")
            ET.SubElement(fileinfo, "number").text = str(i)
            for flag in flags: ET.SubElement(fileinfo, flag)
            fileref = ET.SubElement(workunit, "file_ref")
            ET.SubElement(fileref, "file_number").text = str(i)
            ET.SubElement(fileref, "open_name").text = open_name
            ET.SubElement(fileref, "copy_file")
        template_file = join(tmpdir,'boinc2docker_in_'+uuid().hex)
        open(template_file,'w').write(minidom.parseString(ET.tostring(root, 'utf-8')).toprettyxml(indent=" "*4))

        create_work_args['wu_template'] = template_file
        return create_work(appname, create_work_args, [f for _,f,_ in input_files]).strip()

    except KeyboardInterrupt:
        print("Cleaning up temporary files...")
    finally:
        # cleanup
        try:
            sh("rm -rf {tmpdir}")
        except:
            pass


if __name__=='__main__':

    parser = argparse.ArgumentParser(prog='boinc2docker_create_work')

    #docker args
    parser.add_argument('IMAGE', help='Docker image to run')
    parser.add_argument('COMMAND', nargs=argparse.REMAINDER, metavar='COMMAND', help='command to run')
    parser.add_argument('--entrypoint', help='Overwrite the default ENTRYPOINT of the image')

    #BOINC args
    parser.add_argument('--appname', default='boinc2docker', help='appname (default: boinc2docker)')
    add_create_work_args(parser,exclude=['wu_template'])

    args = parser.parse_args()

    print boinc2docker_create_work(image=args.IMAGE, 
                                   command=args.COMMAND, 
                                   appname=args.appname,
                                   entrypoint=args.entrypoint,
                                   create_work_args=read_create_work_args(args))

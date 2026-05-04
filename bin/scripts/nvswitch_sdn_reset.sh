#!/bin/bash
pdsh -w  $1-nvsw-01 "nv action reset sdn factory-default force"

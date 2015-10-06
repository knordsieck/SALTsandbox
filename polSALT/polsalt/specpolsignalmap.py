
"""
specpolsignalmap

Find Second order, Littrow ghost
Find sky lines and produce sky flat for 2d sky subtraction
Make smoothed psf eighting factor for optimized extraction

"""

import os, sys, glob, shutil, inspect

import numpy as np
import pyfits
from scipy.interpolate import interp1d
from scipy.ndimage.interpolation import shift
from scipy.ndimage import convolve1d
from scipy import linalg as la

import reddir
datadir = os.path.dirname(inspect.getfile(reddir))+"/data/"

from blksmooth2d import blksmooth2d
from pyraf import iraf
from iraf import pysalt
from saltobslog import obslog
from saltsafelog import logging

# np.seterr(invalid='raise')
np.set_printoptions(threshold=np.nan)
debug = True

# ---------------------------------------------------------------------------------
def specpolsignalmap(hdu,logfile=sys.stdout):

    with logging(logfile, debug) as log:
        sci_orc = hdu['sci'].data
        var_orc = hdu['var'].data
        isbadbin_orc = hdu['bpm'].data > 0
        wav_orc = hdu['wav'].data

        lam_m = np.loadtxt(datadir+"wollaston.txt",dtype=float,usecols=(0,))
        rpix_om = np.loadtxt(datadir+"wollaston.txt",dtype=float,unpack=True,usecols=(1,2))

    # trace spectrum, compute spatial profile 
        rows,cols = sci_orc.shape[1:3]
        rbin,cbin = np.array(hdu[0].header["CCDSUM"].split(" ")).astype(int)
        lam_c = wav_orc[0,rows/2]
        profile_orc = np.zeros_like(sci_orc)
        drow_oc = np.zeros((2,cols))
        expectrow_oc = np.zeros((2,cols),dtype='float32')
        maxrow_oc = np.zeros((2,cols),dtype=int); maxval_oc = np.zeros((2,cols),dtype='float32')
        col_cr,row_cr = np.indices(sci_orc[0].T.shape)
        cross_or = np.sum(sci_orc[:,:,cols/2-cols/16:cols/2+cols/16],axis=2)
        
        okprof_oyc = np.ones((2,rows,cols),dtype='bool')
        profile_oyc = np.zeros_like(profile_orc)
        isbadbin_oyc = np.zeros_like(isbadbin_orc)
        var_oyc = np.zeros_like(var_orc)
        wav_oyc = np.zeros_like(wav_orc)
        isnewbadbin_oyc = np.zeros_like(isbadbin_orc)

        for o in (0,1):
        # find spectrum roughly from max of central cut, then within narrow curved aperture
            expectrow_oc[o] = (1-o)*rows + interp1d(lam_m,rpix_om[o],kind='cubic')(lam_c)/rbin    
            crossmaxval = np.max(cross_or[o,expectrow_oc[o,cols/2]-100/rbin:expectrow_oc[o,cols/2]+100/rbin])
            drow = np.where(cross_or[o]==crossmaxval)[0][0] - expectrow_oc[o,cols/2]
            row_c = (expectrow_oc[o] + drow).astype(int)
            aperture_cr = ((row_cr-row_c[:,None])>=-20/rbin) & ((row_cr-row_c[:,None])<=20/rbin)            
            maxrow_oc[o] = np.argmax(sci_orc[o].T[aperture_cr].reshape((cols,-1)),axis=1) + row_c - 20/rbin
            maxval_oc[o] = sci_orc[o,maxrow_oc[o]].diagonal()
            drow1_c = maxrow_oc[o] -(expectrow_oc[o] + drow)
            trow_o = maxrow_oc[:,cols/2]

        # divide out spectrum (allowing for spectral curvature) to make spatial profile
            okprof_c = (maxval_oc[o] != 0)
            drow2_c = np.polyval(np.polyfit(np.where(okprof_c)[0],drow1_c[okprof_c],3),(range(cols)))
            okprof_c &= np.abs(drow2_c - drow1_c) < 3
            norm_rc = np.zeros((rows,cols))
            for r in range(rows):
                norm_rc[r] = interp1d(wav_orc[o,trow_o[o],okprof_c],maxval_oc[o,okprof_c], \
                    bounds_error = False, fill_value=0.)(wav_orc[o,r])
            okprof_rc = (norm_rc != 0.)
            profile_orc[o,okprof_rc] = sci_orc[o,okprof_rc]/norm_rc[okprof_rc]
            var_orc[o,okprof_rc] = var_orc[o,okprof_rc]/norm_rc[okprof_rc]**2
            drow_oc[o] = -(expectrow_oc[o] - expectrow_oc[o,cols/2] + drow2_c -drow2_c[cols/2])

        # take out profile spatial curvature and tilt (r -> y)

            for c in range(cols):
                profile_oyc[o,:,c] = shift(profile_orc[o,:,c],drow_oc[o,c],order=1)
                isbadbin_oyc[o,:,c] = shift(isbadbin_orc[o,:,c].astype(int),drow_oc[o,c],cval=1,order=1) > 0.1
                var_oyc[o,:,c] = shift(var_orc[o,:,c],drow_oc[o,c],order=1)
                wav_oyc[o,:,c] = shift(wav_orc[o,:,c],drow_oc[o,c],order=1)
            okprof_oyc[o] = ~isbadbin_oyc[o] & okprof_rc
            
    # Find, mask off fov edge (profile and image), including possible beam overlap
        profile_oy = np.median(profile_oyc,axis=-1)
        edgerow_od = np.zeros((2,2),dtype=int)
        isbadrow_oy = np.zeros((2,rows),dtype=bool)
        axisrow_o = np.zeros(2)
        maxoverlaprows = 34/rbin                        # for 4' longslit in NIR
        for d,o in np.ndindex(2,2):                         # _d = (0,1) = (bottom,top)
            row_y = np.where((d==1) ^ (np.arange(rows) < trow_o[o]))[0][::2*d-1]
            edgeval = np.median(profile_oy[o,row_y],axis=-1)        
            hist,bin = np.histogram(profile_oy[o,row_y],bins=32,range=(0,edgeval))
            histarg = 32 - np.argmax(hist[::-1]<3)      # edge: <3 in hist in decreasing dirn
            edgeval = bin[histarg]
            edgerow_od[o,d] = trow_o[o] + (2*d-1)*(np.argmax(profile_oy[o,row_y] <= edgeval))
            axisrow_o[o] += edgerow_od[o,d]
            edgerow_od[o,d] = np.clip(edgerow_od[o,d],maxoverlaprows,rows-maxoverlaprows)
            isbadrow_oy[o] |= ((d==1) ^ (np.arange(rows) < (edgerow_od[o,d]+d)))
            okprof_oyc[o,isbadrow_oy[o],:] = False            
            isnewbadbin_oyc[o,isbadrow_oy[o],:] = True 
        axisrow_o /= 2.

        log.message('Optical axis row:  O    %4i     E    %4i' % tuple(axisrow_o), with_header=False)
        log.message('Target center row: O    %4i     E    %4i' % tuple(trow_o), with_header=False)
        log.message('Bottom, top row:   O %4i %4i   E %4i %4i \n' \
                % tuple(edgerow_od.flatten()), with_header=False)
                
    # Mask off atmospheric A- and B-band (profile only) 
        ABband = np.array([[7592.,7667.],[6865.,6878.]])         
        for b in (0,1): okprof_oyc &= ~((wav_oyc>ABband[b,0])&(wav_oyc<ABband[b,1]))

        profile_oyc *= okprof_oyc

    # Stray light search by looking for features in spatial profile of ghost filter                   
        profile_Y = 0.5*(profile_oy[0,trow_o[0]-16:trow_o[0]+17] + \
                        profile_oy[1,trow_o[1]-16:trow_o[1]+17])
        profile_Y = interp1d(np.arange(-16.,17.),profile_Y,kind='cubic')(np.arange(-16.,16.,1./16))

        fwhm = (np.argmax(profile_Y[256:]<0.5) + np.argmax(profile_Y[256:0:-1]<0.5))/16.
        kernelcenter = np.ones(np.around(fwhm/2)*2+2)
        kernelbkg = np.ones(kernelcenter.shape[0]+4)
        kernel = -kernelbkg*kernelcenter.sum()/(kernelbkg.sum()-kernelcenter.sum())
        kernel[2:-2] = kernelcenter                # ghost search kernel is size of fwhm and sums to zero
        ghost_oyc = convolve1d(profile_oyc,kernel,axis=1,mode='constant',cval=0.)
        isbadghost_oyc = (~okprof_oyc | isbadbin_oyc | isnewbadbin_oyc)     

        isbadghost_oyc |= convolve1d(isbadghost_oyc.astype(int),kernelbkg,axis=1,mode='constant',cval=1) != 0
        for o in(0,1): isbadghost_oyc[o,trow_o[o]-3*fwhm/2:trow_o[o]+3*fwhm/2,:] = True

        ghost_oyc *= (~isbadghost_oyc).astype(int)                
        stdghost_oy = np.std(ghost_oyc,axis=-1)
        stdghost = np.median(stdghost_oy)
        boxbins = (int(2.5*fwhm)/2)*2 + 1
        boxrange = np.arange(-int(boxbins/2),int(boxbins/2)+1)

    # Search for second order in ghost filter
        col_C = np.arange(np.argmax(wav_oyc[0,trow_o[0]]/2. > lam_m[0]),cols)
        is2nd_oyc = np.zeros((2,rows,cols),dtype=bool)
        row2nd_oYC = np.zeros((2,boxbins,col_C.shape[0]),dtype=int)

        for o in (0,1):
            row2nd_C = np.around(trow_o[o] + (interp1d(lam_m,rpix_om[o],kind='cubic')(lam_c[col_C]/2.)  \
                - interp1d(lam_m,rpix_om[o],kind='cubic')(lam_c[col_C]))/rbin).astype(int)
            row2nd_oYC[o] = row2nd_C + boxrange[:,None]
            is2nd_oyc[o][row2nd_oYC[o],col_C] = ghost_oyc[o][row2nd_oYC[o],col_C] > 3.*stdghost
            
    # Mask off second order (profile and image), using box found above on profile
        if is2nd_oyc.sum() > 100: 
            is2nd_c = np.any(np.all(is2nd_oyc,axis=0),axis=0)
            col2nd1 = np.where(is2nd_c)[0][-1]
            col2nd0 = col2nd1 - np.where(~is2nd_c[col2nd1::-1])[0][0] +1
            col_C = np.arange(col2nd0,col2nd1+1)
            row2nd_oYC = np.zeros((2,boxbins,col_C.shape[0]),dtype=int)
            is2nd_oyc = np.zeros((2,rows,cols),dtype=bool)        
            for o in (0,1):
                row2nd_C = np.around(trow_o[o] + (interp1d(lam_m,rpix_om[o],kind='cubic')(lam_c[col_C]/2.)  \
                    - interp1d(lam_m,rpix_om[o],kind='cubic')(lam_c[col_C]))/rbin).astype(int)
                row2nd_oYC[o] = row2nd_C + boxrange[:,None]
                for y in np.arange(edgerow_od[o,0],edgerow_od[o,1]+1):
                    profile_oy[o,y] = np.median(profile_oyc[o,y,okprof_oyc[o,y,:]])
                dprofile_yc = profile_oyc[o] - profile_oy[o,:,None]
                is2nd_oyc[o][row2nd_oYC[o],col_C] = \
                    (dprofile_yc[row2nd_oYC[o],col_C] > 5.*np.sqrt(var_oyc[o][row2nd_oYC[o],col_C]))
                strength2nd = dprofile_yc[is2nd_oyc[o]].max()
            is2nd_c = np.any(np.all(is2nd_oyc,axis=0),axis=0)
            col2nd1 = np.where(is2nd_c)[0][-1]
            col2nd0 = col2nd1 - np.where(~is2nd_c[col2nd1::-1])[0][0] +1
            is2nd_oyc[:,:,0:col2nd0-1] = False
            wav2nd0,wav2nd1 = wav_oyc[0,trow_o[0],[col2nd0,col2nd1]]
            okprof_oyc &= (~is2nd_oyc)
            isnewbadbin_oyc |= is2nd_oyc
            isbadghost_oyc |= is2nd_oyc
            ghost_oyc *= (~isbadghost_oyc).astype(int)                     
#            pyfits.PrimaryHDU(isbadghost_oyc.astype('uint8')).writeto('isbadghost_oyc.fits',clobber=True) 
#            pyfits.PrimaryHDU(ghost_oyc.astype('float32')).writeto('ghost_oyc.fits',clobber=True) 

            log.message('2nd order masked,     strength %7.4f, wavel %7.1f - %7.1f (/2)' \
                        % (strength2nd,wav2nd0,wav2nd1), with_header=False)        

    # Remaining ghosts have same position O and E. _Y = row around target
        Rows = 2*np.abs(trow_o[:,None]-edgerow_od).min()+1
        row_oY = np.add.outer(trow_o,np.arange(Rows)-Rows/2)
        ghost_Yc = 0.5*ghost_oyc[np.arange(2)[:,None],row_oY,:].sum(axis=0) 
        isbadghost_Yc = isbadghost_oyc[np.arange(2)[:,None],row_oY,:].any(axis=0)
        profile_Yc = 0.5*profile_oyc[np.arange(2)[:,None],row_oY,:].sum(axis=0) 
        okprof_Yc = okprof_oyc[np.arange(2)[:,None],row_oY,:].all(axis=0)               
        
    # Search for Littrow ghost as undispersed object off target

        isbadlitt_Yc = isbadghost_Yc | \
            (convolve1d(isbadghost_Yc.astype(int),kernelbkg,axis=1,mode='constant',cval=1) != 0)
        litt_Yc = convolve1d(ghost_Yc,kernel,axis=-1,mode='constant',cval=0.)*(~isbadlitt_Yc).astype(int)
#        pyfits.PrimaryHDU(litt_Yc.astype('float32')).writeto('litt_Yc.fits',clobber=True)
#        pyfits.PrimaryHDU(isbadlitt_Yc.astype('uint8')).writeto('isbadlitt_Yc.fits',clobber=True)
        islitt_Yc = litt_Yc > 10.*stdghost    
        Rowlitt,collitt = np.argwhere(litt_Yc == litt_Yc[:col2nd0].max())[0]

        littbox_Yc = np.meshgrid(boxrange+Rowlitt,boxrange+collitt)
        
    # Mask off Littrow ghost (profile and image)
        if islitt_Yc[littbox_Yc].sum() > 2:
            islitt_oyc = np.zeros((2,rows,cols),dtype=bool)
            for o in (0,1):
                for y in np.arange(edgerow_od[o,0],edgerow_od[o,1]+1):
                    profile_oy[o,y] = np.median(profile_oyc[o,y,okprof_oyc[o,y,:]])
                dprofile_yc = profile_oyc[o] - profile_oy[o,:,None]
                littbox_yc = np.meshgrid(boxrange+Rowlitt-Rows/2+trow_o[o],boxrange+collitt)
                islitt_oyc[o][littbox_yc] =  \
                    dprofile_yc[littbox_yc] > 10.*np.sqrt(var_oyc[o][littbox_yc])
            wavlitt = wav_oyc[0,trow_o[0],collitt]
            strengthlitt = dprofile_yc[littbox_yc].max()
            okprof_oyc[islitt_oyc] = False
            isnewbadbin_oyc |= islitt_oyc
            isbadghost_Yc[littbox_Yc] = True
            ghost_Yc[littbox_Yc] = 0.
            
            log.message('Littrow ghost masked, strength %7.4f, ypos %5.1f", wavel %7.1f' \
                        % (strengthlitt,(Rowlitt-Rows/2)*(rbin/8.),wavlitt), with_header=False)        
            
    # Anything left as spatial profile feature is assumed to be neighbor non-target stellar spectrum
        okprof_Yc = okprof_oyc[np.arange(2)[:,None],row_oY,:].all(axis=0)
        okprof_Y = okprof_Yc.any(axis=1)
        profile_Y = np.zeros(Rows,dtype='float32')
        for Y in np.where(okprof_Y)[0]: profile_Y[Y] = np.median(profile_Yc[Y,okprof_Yc[Y]])
        avoid = int(np.around(fwhm/2)) +3
        okprof_Y[range(avoid) + range(Rows/2-avoid,Rows/2+avoid) + range(Rows-avoid,Rows)] = False
        nbr_Y = convolve1d(profile_Y,kernel,mode='constant',cval=0.)
        Yownbr = np.where(nbr_Y==nbr_Y[okprof_Y].max())[0]
        strengthnbr = nbr_Y[Yownbr]

        log.message('Brightest neighbor:   strength %7.4f, ypos %5.1f"' \
                            % (strengthnbr,(Yownbr-Rows/2)*(rbin/8.)), with_header=False)         

    # now locate sky lines
    # find profile continuum background starting with block median, find skylines above 3-sigma 
        rblks,cblks = (256,16)
        rblk,cblk = rows/rblks,cols/cblks
        bkg_oyc = np.zeros((2,rows,cols))
        for o in (0,1):          
            bkg_oyc[o] = blksmooth2d(profile_oyc[o],okprof_oyc[o],rblk,cblk,0.5,mode="median")
        okprof_oyc &= (bkg_oyc > 0.)&(var_oyc > 0)
        skyline_oyc = (profile_oyc - bkg_oyc)*okprof_oyc
        isline_oyc = np.zeros((2,rows,cols),dtype=bool)
        isline_oyc[okprof_oyc] = (skyline_oyc[okprof_oyc] > 3.*np.sqrt(var_oyc[okprof_oyc]))
#        pyfits.PrimaryHDU(bkg_oyc.astype('float32')).writeto('bkg_oyc_0.fits',clobber=True) 
#        pyfits.PrimaryHDU(skyline_oyc.astype('float32')).writeto('skyline_oyc_0.fits',clobber=True) 
#        pyfits.PrimaryHDU(isline_oyc.astype('uint8')).writeto('isline_oyc_0.fits',clobber=True) 
                   
    # iterate once, using mean outside of skylines      
        for o in (0,1):
            bkg_oyc[o] = blksmooth2d(profile_oyc[o],(okprof_oyc & ~isline_oyc & ~isbadrow_oy[:,:,None])[o], \
                    rblk,cblk,0.25,mode="mean")
        okprof_oyc &= (bkg_oyc > 0.)&(var_oyc > 0)
        
    # remove continuum, refinding skylines   
        skyline_oyc = (profile_oyc - bkg_oyc)*okprof_oyc
        isline_oyc = np.zeros((2,rows,cols),dtype=bool)
        isline_oyc[okprof_oyc] = (skyline_oyc[okprof_oyc] > 3.*np.sqrt(var_oyc[okprof_oyc]))

    # map sky lines and compute sky flat for each:
    # first mask out non-background rows
        linebins_oy = isline_oyc.sum(axis=2)
        median_oy = np.median(linebins_oy,axis=1)[:,None]
        isbadrow_oy |= (linebins_oy == 0) | (linebins_oy > 1.5*median_oy)
        isline_oyc[isbadrow_oy,:] = False
    # count up linebins at each wavelength
        wavmin,wavmax = wav_oyc[:,rows/2,:].min(),wav_oyc[:,rows/2,:].max()
        dwav = np.around((wavmax-wavmin)/cols,1)
        wavmin,wavmax = np.around([wavmin,wavmax],1)
        wav_w = np.arange(wavmin,wavmax,dwav)
        wavs = wav_w.shape[0]
        argwav_oyc = ((wav_oyc-wavmin)/dwav).astype(int)
        wavhist_w = (argwav_oyc[isline_oyc][:,None] == np.arange(wavs)).sum(axis=0)
    # find line wavelengths where line bins exceed 50% of background rows in more than one column     
        thresh = wavhist_w.max()/2
        argwav1_l = np.where((wavhist_w[:-1]<thresh) & (wavhist_w[1:]>thresh))[0] + 1
        argwav2_l = np.where((wavhist_w[argwav1_l[0]:-1]>thresh) & \
                    (wavhist_w[argwav1_l[0]+1:]<thresh))[0] + argwav1_l[0]
        lines = min(argwav1_l.shape[0],argwav2_l.shape[0])
        if argwav2_l.shape[0] > lines: argwav2_l = np.resize(argwav2_l,lines) 
        cols_l = np.around((wav_w[argwav2_l]-wav_w[argwav1_l])/dwav).astype(int)
        if (cols_l <2).sum():
            argwav1_l = np.delete(argwav1_l,np.where(cols_l<2)[0])
            argwav2_l = np.delete(argwav2_l,np.where(cols_l<2)[0])
            cols_l = np.delete(cols_l,np.where(cols_l<2)[0])
            lines = cols_l.shape[0] 
                                 
    # make row,col map of line locations, extract spatial line profile, polynomial smoothed               
        dwav1_loyc = np.abs(wav_oyc - wav_w[argwav1_l][:,None,None,None])
        col1_loy = np.argmin(dwav1_loyc,axis=-1)       
        line_oyc = -1*np.ones((2,rows,cols),dtype=int)
        int_loy = np.zeros((lines,2,rows)); intsm_loy = np.zeros_like(int_loy)
        intvar_lo = np.zeros((lines,2))          
        corerows = ((profile_Y - profile_Y[profile_Y>0].min()) > 0.01).sum()
        isbadrow_oy |= (np.abs((np.arange(rows) - trow_o[:,None])) < corerows/2)     

        for l,o in np.ndindex(lines,2):
            col_yc = np.add.outer(col1_loy[l,o],np.arange(cols_l[l])).astype(int)
            line_oyc[o][np.arange(rows)[:,None],col_yc] = l
            okrow_y = okprof_oyc[o][np.arange(rows)[:,None],col_yc].all(axis=1) & ~isbadrow_oy[o]
            inrow_y = np.where(okrow_y)[0]
            int_loy[l,o] = (skyline_oyc[o][np.arange(rows)[:,None],col_yc].sum(axis=1)) * okrow_y
            outrow_y = np.arange(inrow_y.min(),inrow_y.max()+1)       
            a = np.vstack((inrow_y**2,inrow_y,np.ones(inrow_y.shape[0]))).T
            b = int_loy[l,o,inrow_y]                      
            polycof = la.lstsq(a,b)[0]
            axisval = np.polyval(polycof,axisrow_o[o])  
            intsm_loy[l,o,outrow_y] = np.polyval(polycof,outrow_y)/axisval
            int_loy[l,o] /= axisval
            intvar_lo[l,o] = (int_loy[l,o] - intsm_loy[l,o])[okrow_y].var()
                 
    # form skyflat from sky lines, 2D polynomial weighted by line fit 1/variance
    # only use lines id'd in UVES sky linelist
        uvesfluxlim = 1.
        lam_u,flux_u = np.loadtxt(datadir+"uvesskylines.txt",dtype=float,unpack=True,usecols=(0,3))
        isid_ul = ((lam_u[:,None] > wav_w[argwav1_l]) & (lam_u[:,None] < wav_w[argwav2_l]))
        fsky_l = (flux_u[:,None]*isid_ul).sum(axis=0)
        wav_l = 0.5*(wav_w[argwav1_l]+wav_w[argwav2_l])

        line_s = np.where(fsky_l > uvesfluxlim)[0]
        skylines = line_s.shape[0]
        line_m = np.where(fsky_l <= uvesfluxlim)[0]
        masklines = line_m.shape[0]
        if masklines>0:
            wavautocol = 6697.
            isautocol_m = ((wavautocol > wav_w[argwav1_l[line_m]]) \
                         & (wavautocol < wav_w[argwav2_l[line_m]]))
            if isautocol_m.sum():
                lineac = line_m[np.where(isautocol_m)[0]]
                isnewbadbin_oyc |= (line_oyc == lineac)
                
                log.message(('Autocoll laser masked: %6.1f - %6.1f Ang') \
                    % (wav_w[argwav1_l[lineac]],wav_w[argwav2_l[lineac]]), with_header=False)   
                line_m = np.delete(line_m,np.where(isautocol_m)[0])

        masklines = line_m.shape[0]
        if masklines>0:                                        
            wav_m = 0.5*(wav_w[argwav1_l[line_m]]+wav_w[argwav2_l[line_m]])
            okneb_oy = (np.abs(np.arange(rows) - trow_o[:,None]) < rows/6)                             
            isnewbadbin_oyc |= ~okneb_oy[:,:,None] & (line_oyc == line_m[:,None,None,None]).any(axis=0)

            log.message(('Nebula partial mask:   '+masklines*'%6.1f '+' Ang') \
                        % tuple(wav_m), with_header=False)

        skyflat_orc = np.ones((2,rows,cols)) 
                            
        if skylines>0:
            wav_s = ((lam_u*flux_u)[:,None]*isid_ul).sum(axis=0)[line_s]/fsky_l[line_s]

            targetrow_od = np.zeros((2,2))
                          
            log.message('\nSkyflat formed from %3i sky lines %5.1f - %5.1f Ang' \
                        % (skylines,wav_s.min(),wav_s.max()), with_header=False)

            Cofs = 3
            Coferrlim = 0.005
            if skylines <4: Cofs = 2
            for o in (0,1):
                int_ys = int_loy[line_s,o].T
                iny_f,ins_f = np.where(int_ys > 0)
                points = iny_f.shape[0]
                inpos_f = (iny_f - axisrow_o[o])/(0.5*rows)
                inwav_f = (wav_s[ins_f]-wav_w.mean())/(0.5*(wavmax - wavmin))
                invar_f = intvar_lo[line_s[ins_f],o]
                ain_fC = (np.vstack((inpos_f,inpos_f**2,inpos_f*inwav_f,inpos_f**2*inwav_f))).T                    
                bin_f = (int_ys[int_ys > 0] - 1.).flatten() 
                while 1:                   
                    lsqcof_C = la.lstsq(ain_fC[:,:Cofs]/invar_f[:,None],bin_f/invar_f)[0]
                    alpha_CC = (ain_fC[:,:Cofs,None]*ain_fC[:,None,:Cofs]/invar_f[:,None,None]).sum(axis=0)
                    err_C = np.sqrt(np.diagonal(la.inv(alpha_CC)))
                    print "Coefficients: %1s :" % ["O","E"][o],
                    print  (Cofs*"%7.4f +/- %7.4f  ") % tuple(np.vstack((lsqcof_C, err_C)).T.flatten())
                    if (o==1) | (Cofs<=2) | (err_C.max()<Coferrlim):    break
                    Cofs -= 1
                    
            # compute skyflat in original geometry
                outr_f,outc_f = np.indices((rows,cols)).reshape((2,-1))
                drow_f = np.broadcast_arrays(drow_oc[o],np.zeros((rows,cols)))[0].flatten()
                outrow_f = (outr_f - axisrow_o[o] + drow_f)/(0.5*rows)
                outwav_f = (wav_orc[o].flatten()-wav_w.mean())/(0.5*(wavmax - wavmin))                                
                aout_fC = (np.vstack((outrow_f,outrow_f**2,outrow_f*outwav_f,outrow_f**2*outwav_f))).T             
                skyflat_orc[o] = (np.dot(aout_fC[:,:Cofs],lsqcof_C) + 1.).reshape((rows,cols))

    # compute smoothed psf, new badpixmap, map of background continuum in original geometry                
        isbkgcont_oyc = ~(isnewbadbin_oyc | isline_oyc | isbadrow_oy[:,:,None])
        isnewbadbin_orc = np.zeros_like(isbadbin_orc)
        isbkgcont_orc = np.zeros_like(isbadbin_orc)
        psf_orc = np.zeros_like(profile_orc)      
        isedge_oyc = (np.arange(rows)[:,None] < edgerow_od[:,None,None,0]) | \
            (np.indices((rows,cols))[0] > edgerow_od[:,None,None,1])        
        isskycont_oyc = (((np.arange(rows)[:,None] < edgerow_od[:,None,None,0]+rows/16) |  \
            (np.indices((rows,cols))[0] > edgerow_od[:,None,None,1]-rows/16)) & ~isedge_oyc)
        for o in (0,1):                         # yes, it's not quite right to use skyflat_o*r*c                      
            skycont_c = (bkg_oyc[o].T[isskycont_oyc[o].T]/ \
                skyflat_orc[o].T[isskycont_oyc[o].T]).reshape((cols,-1)).mean(axis=-1)
            skycont_yc = skycont_c*skyflat_orc[o]           
            profile_oyc[o] -= skycont_yc
            rblk = 1; cblk = int(cols/16)
            profile_oyc[o] = blksmooth2d(profile_oyc[o],(okprof_oyc[o] & ~isline_oyc[o]),   \
                        rblk,cblk,0.25,mode='mean')              
            for c in range(cols):
                psf_orc[o,:,c] = shift(profile_oyc[o,:,c],-drow_oc[o,c],cval=0,order=1)
                isbkgcont_orc[o,:,c] = shift(isbkgcont_oyc[o,:,c].astype(int),-drow_oc[o,c],cval=0,order=1) > 0.1
                isnewbadbin_orc[o,:,c] = shift(isnewbadbin_oyc[o,:,c].astype(int),-drow_oc[o,c],cval=1,order=1) > 0.1
            targetrow_od[o,0] = trow_o[o] - np.argmax(isbkgcont_orc[o,trow_o[o]::-1,cols/2] > 0)
            targetrow_od[o,1] = trow_o[o] + np.argmax(isbkgcont_orc[o,trow_o[o]:,cols/2] > 0)
        maprow_od = np.vstack((edgerow_od[:,0],targetrow_od[:,0],targetrow_od[:,1],edgerow_od[:,1])).T
        maprow_od += np.array([-2,-2,2,2])

        return psf_orc,skyflat_orc,isnewbadbin_orc,isbkgcont_orc,maprow_od,drow_oc
                        
#---------------------------------------------------------------------------------------
if __name__=='__main__':
    infilelist=sys.argv[1:]
    for file in infilelist:
        hdulist = pyfits.open(file)
        name = os.basename(file).split('.')[0]
        psf_orc,skyflat_orc,isnewbadbin_orc,maprow_od,drow_oc = specpolsignalmap(hdulist)
        pyfits.PrimaryHDU(psf_orc.astype('float32')).writeto(name+'_psf.fits',clobber=True) 
        pyfits.PrimaryHDU(skyflat_orc.astype('float32')).writeto(name+'_skyflat.fits',clobber=True) 

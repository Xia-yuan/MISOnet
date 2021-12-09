from itertools import permutations
import torch
import time
import pdb
from pathlib import Path
import os
import numpy as np
import scipy.io.wavfile as wf
import scipy
from pathlib import Path
import soundfile as sf
from numpy.linalg import solve


class Tester_Separate(object):
    def __init__(self,dataset,tr_loader, dt_loader, test_loader, model, device, num_spks, chunk_time, save_rootDir, ref_ch, cuda_flag, tr_inference_flag, **ISTFT_args):

        self.dataset = dataset
        self.tr_loader = tr_loader
        self.dt_loader = dt_loader
        self.test_loader = test_loader
        self.model = model
        self.device = device
        self.num_spks = num_spks
        
        #ISTFT params
        self.fs = ISTFT_args['fs']
        self.window = ISTFT_args['window']
        self.nperseg = ISTFT_args['length']
        self.noverlap = ISTFT_args['overlap'] 
        
        hann_win = scipy.signal.get_window(self.window, self.nperseg)
        self.scale = np.sqrt(1.0/hann_win.sum()**2)
        self.MaxINT16 = np.iinfo(np.int16).max

        self.chunk_size = int(chunk_time * self.fs)
        self.save_rootDir = save_rootDir
        self.ref_ch = ref_ch
        self.cuda_flag = cuda_flag
        self.tr_inference_flag = tr_inference_flag

    def test(self):
        print('testing...')

        if self.tr_inference_flag:
            print('-'*85)
            print(' train dataset inference...')
            print('-'*85)
            saveDir = os.path.join(self.save_rootDir, 'train_si284')
            Path(saveDir).mkdir(exist_ok=False, parents=True)
            self.inference(self.tr_loader, saveDir)
            print('train dataset inference finish !')

        else:
            print('-'*85)
            print('development dataset inference...')
            print('-'*85)
            saveDir = os.path.join(self.save_rootDir,'cv_dev93')
            Path(saveDir).mkdir(exist_ok=False, parents=True)
            self.inference(self.dt_loader, saveDir)
            print('development dataset inference finish !')

            print('-'*85)
            print('test dataset inference...')
            print('-'*85)
            saveDir = os.path.join(self.save_rootDir, 'test_eval92')
            Path(saveDir).mkdir(exist_ok=False, parents=True)
            self.inference(self.test_loader,saveDir)
            print('test dataset inference finish !')

    def inference(self,data_loader,saveDir):
        """
            split_observe_dict : dictionary, keys : split index
                                split_observe_dict[split_index] : [B,Ch,F,T]
            split_clean_s0_dict :        "
            split_clean_s1_dict :        "

            gap : zero pad size of last split wav,  [B]
            wav_name : file name to test
        """
        for idx, (data) in enumerate(data_loader):
            split_observe_dict, split_clean_s0_dict, split_clean_s1_dict, gap, wav_name = data
            split_len = len(split_observe_dict)
            

            for split_idx in range(split_len):
                
                observe = split_observe_dict[str(split_idx)]
                if self.cuda_flag:
                    observe = observe.cuda(self.device)
                observe = torch.roll(observe, -self.ref_ch, dims=1) #[B,Ch,T,F]
                B,Ch,T,F = observe.size()
                if split_idx == 0:
                    t_e_clean_s0 = np.empty([B], dtype=np.ndarray)
                    t_e_clean_s1 = np.empty([B], dtype=np.ndarray)
                
                s0 = torch.unsqueeze(split_clean_s0_dict[str(split_idx)][:,self.ref_ch,:,:],dim=1) #[B,1,T,F]
                s1 = torch.unsqueeze(split_clean_s1_dict[str(split_idx)][:,self.ref_ch,:,:],dim=1) 

                clean = torch.cat((s0,s1), dim=1) #[B,Spks, T, F] 
                s_clean = torch.unsqueeze(clean,dim=2) #[B,1,Spks,T,F]

                e_clean = self.model(observe) #[B,Spks,T,F]
                e_clean = e_clean.detach().cpu()
                s_e_clean = torch.unsqueeze(e_clean, dim=1) #[B,Spks,1,T,F]
                magnitude_e_clean = torch.abs(torch.sqrt(s_e_clean.real**2 + s_e_clean.imag**2)) 

                magnitude_distance = torch.sum(torch.abs(magnitude_e_clean - abs(s_clean)), [3,4]) #[B,Spks,Spks]
                perms = clean.new_tensor(list(permutations(range(self.num_spks))), dtype=torch.long)
                index_ = torch.unsqueeze(perms, dim=2)
                perms_one_hot = clean.new_zeros((*perms.size(), self.num_spks), dtype=torch.float).scatter_(2,index_,1)
                batchwise_distance = torch.einsum('bij,pij->bp', [magnitude_distance,perms_one_hot])
                min_distance_idx = torch.argmin(batchwise_distance, dim=1)

                e_clean_s0 = torch.empty(B,1,T,F)
                e_clean_s1 = torch.empty(B,1,T,F)

                t_split_e_clean_s0 = torch.empty([])
                
                for b_idx in range(B):
                    if min_distance_idx[b_idx] == 1:
                        e_clean_s0 = e_clean[b_idx,1,:,:]
                        e_clean_s1 = e_clean[b_idx,0,:,:]
                    else:
                        e_clean_s0 = e_clean[b_idx,0,:,:]
                        e_clean_s1 = e_clean[b_idx,1,:,:]
                
                    t_split_e_clean_s0 = self.ISTFT(torch.permute(e_clean_s0,[1,0]) * self.scale)
                    t_split_e_clean_s0 = t_split_e_clean_s0 * self.MaxINT16
                    t_split_e_clean_s0 = t_split_e_clean_s0.astype(np.int16)
                    assert t_split_e_clean_s0.shape[0] == self.chunk_size, ('estimate source 0 wav length does not match chunk size, please check ISTFT function ') 

                    t_split_e_clean_s1 = self.ISTFT(torch.permute(e_clean_s1,[1,0]) * self.scale)
                    t_split_e_clean_s1 = t_split_e_clean_s1 * self.MaxINT16
                    t_split_e_clean_s1 = t_split_e_clean_s1.astype(np.int16)
                    assert t_split_e_clean_s1.shape[0] == self.chunk_size, ('estimate source 1 wav length does not match chunk size, please check ISTFT function ') 

                    if split_idx == split_len -1:
                        t_split_e_clean_s0 = t_split_e_clean_s0[:len(t_split_e_clean_s0)-gap[b_idx]]
                        t_split_e_clean_s1 = t_split_e_clean_s1[:len(t_split_e_clean_s1)-gap[b_idx]]

                    if split_idx == 0:
                        t_e_clean_s0[b_idx] = t_split_e_clean_s0
                        t_e_clean_s1[b_idx]=  t_split_e_clean_s1
                    else:
                        t_e_clean_s0[b_idx] = np.append(t_e_clean_s0[b_idx] ,t_split_e_clean_s0)
                        t_e_clean_s1[b_idx] = np.append(t_e_clean_s1[b_idx], t_split_e_clean_s1)
                    
            for b_idx in range(B):
                sf.write('{}_0.wav'.format(os.path.join(saveDir,wav_name[b_idx])), t_e_clean_s0[b_idx].T, self.fs, 'PCM_24')
                sf.write('{}_1.wav'.format(os.path.join(saveDir,wav_name[b_idx])), t_e_clean_s1[b_idx].T, self.fs, 'PCM_24')
            print('[MISO1] Testing ... ','Save Directory : {}'.format(saveDir), 'process : {:.2f}'.format(idx/len(data_loader)))


    def ISTFT(self,FT_sig): 

        '''
        input : [F,T]
        output : [T,C]
        '''
        # if FT_sig.shape[1] != self.config['ISTFT']['length']+1:
            # FT_sig = np.transpose(FT_sig,(0,1)) # [C,T,F] -> [C,F,T]

        _, t_sig = scipy.signal.istft(FT_sig,fs=self.fs, window=self.window, nperseg=self.nperseg, noverlap=self.noverlap) #[C,F,T] -> [T,C]

        return t_sig





class Tester_Beamforming(object):
    def __init__(self,dataset,tr_loader, dt_loader,test_loader,model,device,num_spks,chunk_time,save_rootDir,ref_ch, cuda_flag, tr_inference_flag, **ISTFT_args):
        
        self.dataset = dataset
        self.tr_loader = tr_loader
        self.dt_loader = dt_loader
        self.test_loader = test_loader
        self.model = model
        self.device = device
        self.num_spks = num_spks

        # ISTFT params
        self.fs = ISTFT_args['fs']
        self.window = ISTFT_args['window']
        self.nperseg = ISTFT_args['length']
        self.noverlap = ISTFT_args['overlap']

        hann_win = scipy.signal.get_window(self.window, self.nperseg)
        self.scale = np.sqrt(1.0/hann_win.sum()**2)
        self.MaxInt16 = np.iinfo(np.int16).max

        self.chunk_size = int(chunk_time * self.fs)
        self.save_rootDir = save_rootDir
        self.ref_ch = ref_ch
        self.cuda_flag = cuda_flag
        self.tr_inference_flag =tr_inference_flag

    def test(self):
        print('testing...')
        if self.tr_inference_flag:
            print('-'*85)
            print(' train dataset inference...')
            print('-'*85)
            saveDir = os.path.join(self.save_rootDir, 'train_si284')
            Path(saveDir).mkdir(exist_ok=False, parents=True)
            self.inference(self.tr_loader, saveDir)
            print('train dataset inference finish !')

        else:
            print('-'*85)
            print('development dataset inference ...')
            print('-'*85)
            saveDir = os.path.join(self.save_rootDir, 'cv_dev93')
            Path(saveDir).mkdir(exist_ok=True, parents=True)
            self.inference(self.dt_loader,saveDir)
            print('development dataset inference finish !')

            print('-'*85)
            print('test dataset inference...')
            print('-'*85)
            saveDir = os.path.join(self.save_rootDir, 'test_eval92')
            Path(saveDir).mkdir(exist_ok=True, parents=True)
            self.inference(self.test_loader, saveDir)
            print('test dataset inference finish !')

    def inference(self, data_loader, saveDir):
        """
            split_observe_dict : dictionary, keys : split index
                                split_observe_dict[split_index] : [B,Ch,F,T]
            split_clean_s0_dict :       "
            split_clean_s1_dict :       "

            gap : zero pad size of last split wav, [B]
            wav_name : file name to test
        """

        for idx, (data) in enumerate(data_loader):
            split_observe_dict, split_clean_s0_dict, split_clean_s1_dict, gap, wav_name = data
            split_len = len(split_observe_dict)


            for split_idx in range(split_len):
                
                observe = split_observe_dict[str(split_idx)] #[B,Ch,F,T]
                if self.cuda_flag:
                    observe = observe.cuda(self.device)
                B, Ch, T, F = observe.size()

                if split_idx == 0:
                    t_e_clean_s0 = np.empty([B], dtype=np.ndarray)
                    t_e_clean_s1 = np.empty([B], dtype=np.ndarray)
                
                s0 = split_clean_s0_dict[str(split_idx)][:,self.ref_ch,:,:] # [B,1,T,F]
                s1 = split_clean_s1_dict[str(split_idx)][:,self.ref_ch,:,:]
                clean = torch.stack((s0,s1), dim=1) #[B,Ch,T,F]
                s_clean = torch.unsqueeze(clean, dim=2) #[B,Ch,1,T,F]
            
                """
                    Apply Source Separation
                """
                e_clean_MISO1_temp = self.MISO1_Inference(observe, ref_ch=self.ref_ch)
                if self.cuda_flag:
                    observe = observe.detach().cpu()
                    for spk_idx in range(self.num_spks):
                        e_clean_MISO1_temp[spk_idx] = e_clean_MISO1_temp[spk_idx].detach().cpu()
                
                """
                    Source Alignment between Clean reference signal and MISO1 signal 
                    calculate magnitude distance between ref mic(ch0) and target signal(reference mic : ch0) 
                """
                e_clean_MISO1 = [[] for _ in range(self.num_spks)]
                for spk_idx in range(self.num_spks):
                    e_clean_MISO1[spk_idx] = torch.zeros_like(e_clean_MISO1_temp[spk_idx])
                    if spk_idx == 0:
                        e_clean_MISO1_ref_ch = e_clean_MISO1_temp[spk_idx][:,self.ref_ch,:,:] #[B,T,F]
                    else:
                        e_clean_MISO1_ref_ch = torch.stack((e_clean_MISO1_ref_ch, e_clean_MISO1_temp[spk_idx][:,self.ref_ch,:,:]),dim=1)
                # MISO1_stft_ref_ch : [B,Spks,T,F]

                s_e_clean_MISO1_ref_ch = torch.unsqueeze(e_clean_MISO1_ref_ch, dim=1) #[B,1,Spks,T,F]
                magnitude_e_clean_MISO1_ref_ch = torch.abs(torch.sqrt(s_e_clean_MISO1_ref_ch.real**2 + s_e_clean_MISO1_ref_ch.imag**2))
                magnitude_distance = torch.sum(torch.abs(magnitude_e_clean_MISO1_ref_ch - abs(s_clean)),[3,4])
                perms = clean.new_tensor(list(permutations(range(self.num_spks))), dtype=torch.long)
                index_ = torch.unsqueeze(perms, dim=2)
                perms_one_hot = clean.new_zeros((*perms.size(), self.num_spks), dtype=torch.float).scatter_(2,index_,1)
                batchwise_distance = torch.einsum('bij,pij->bp', [magnitude_distance, perms_one_hot])
                min_distance_idx = torch.argmin(batchwise_distance, dim=1)

                for b_idx in range(B):
                    align_index = torch.argmax(perms_one_hot[min_distance_idx[b_idx]], dim=1)
                    for spk_idx in range(self.num_spks):
                        target_index = align_index[spk_idx]
                        e_clean_MISO1[spk_idx][b_idx,...] = e_clean_MISO1_temp[target_index][b_idx,...]
                
                """
                    Apply MVDR Beamforming
                """

                e_clean_Beamformer = [[] for _ in range(self.num_spks)]
                observe = torch.permute(observe, [0,3,1,2]).numpy()
                for spk_idx in range(self.num_spks):
                    source = torch.permute(e_clean_MISO1[spk_idx], [0,3,1,2]).numpy()
                    e_clean_Beamformer[spk_idx] = torch.squeeze(self.Apply_Beamforming(source,observe),dim=1)
                
                for b_idx in range(B):
                    t_split_e_clean_s0 = self.ISTFT(torch.permute(e_clean_Beamformer[0][b_idx,...],[1,0]) * self.scale)
                    t_split_e_clean_s0 = t_split_e_clean_s0 * self.MaxInt16
                    t_split_e_clean_s0 = t_split_e_clean_s0.astype(np.int16)
                    assert t_split_e_clean_s0.shape[0] == self.chunk_size, ('estimate source 0 wav length does not match chunk size, please check ISTFT function ') 

                    t_split_e_clean_s1 = self.ISTFT(torch.permute(e_clean_Beamformer[1][b_idx,...],[1,0]) * self.scale)
                    t_split_e_clean_s1 = t_split_e_clean_s1 * self.MaxInt16
                    t_split_e_clean_s1 = t_split_e_clean_s1.astype(np.int16)
                    assert t_split_e_clean_s1.shape[0] == self.chunk_size, ('estimate source 1 wav length does not match chunk size, please check ISTFT function ') 

                    if split_idx == split_len -1:
                        t_split_e_clean_s0 = t_split_e_clean_s0[:len(t_split_e_clean_s0)-gap[b_idx]]
                        t_split_e_clean_s1 = t_split_e_clean_s1[:len(t_split_e_clean_s1)-gap[b_idx]]

                    if split_idx == 0:
                        t_e_clean_s0[b_idx] = t_split_e_clean_s0
                        t_e_clean_s1[b_idx]=  t_split_e_clean_s1
                    else:
                        t_e_clean_s0[b_idx] = np.append(t_e_clean_s0[b_idx] ,t_split_e_clean_s0)
                        t_e_clean_s1[b_idx] = np.append(t_e_clean_s1[b_idx], t_split_e_clean_s1)

            for b_idx in range(B):
                sf.write('{}_0.wav'.format(os.path.join(saveDir,wav_name[b_idx])), t_e_clean_s0[b_idx].T, self.fs, 'PCM_24')
                sf.write('{}_1.wav'.format(os.path.join(saveDir,wav_name[b_idx])), t_e_clean_s1[b_idx].T, self.fs, 'PCM_24')
            print('[Beamforming] Testing ... ','Save Directory : {}'.format(saveDir), 'process : {:.2f}'.format(idx/len(data_loader)))
                        
    def ISTFT(self,FT_sig): 

        '''
        input : [F,T]
        output : [T,C]
        '''
        # if FT_sig.shape[1] != self.config['ISTFT']['length']+1:
            # FT_sig = np.transpose(FT_sig,(0,1)) # [C,T,F] -> [C,F,T]

        _, t_sig = scipy.signal.istft(FT_sig,fs=self.fs, window=self.window, nperseg=self.nperseg, noverlap=self.noverlap) #[C,F,T] -> [T,C]

        return t_sig


    def MISO1_Inference(self,mix_stft,ref_ch=0):
        """
        Input:
            mix_stft : observe STFT, size - [B, Mic, T, F]
        Output:
            MISO1_stft : list of separated source, - [B, reference Mic, T, F]

            1. circular shift the microphone array at run time for the prediction of each microphone signal
               If the microphones are arranged uniformly on a circle, Select the reference microphone by circular shifting the microphone. e.g reference mic q -> [Yq, Yq+1, ..., Yp, Y1, ..., Yq-1]
            2. Using Permutation Invariance Alignmnet method to match between clean target signal and estimated signal
        """
        B, M, T, F = mix_stft.size()

        MISO1_stft = [torch.empty(B,M,T,F, dtype=torch.complex64) for _ in range(self.num_spks)]
        
        Mic_array = [x for x in range(M)]
        Mic_array = np.roll(Mic_array, -ref_ch)  # [ref_ch, ref_ch+1, ..., 0, 1, ..., ref_ch-1]
        # print('Mic_array : ', Mic_array)

        with torch.no_grad():
            mix_stft_refCh = torch.roll(mix_stft,-ref_ch, dims=1)
            MISO1_refCh = self.model(mix_stft_refCh)

        for spk_idx in range(self.num_spks):
            MISO1_stft[spk_idx][:,ref_ch,...] = MISO1_refCh[:,spk_idx,...]
            
        # MISO1_spk1[:,ref_ch,...] = MISO1_refCh[:,0,...]
        # MISO1_spk2[:,ref_ch,...] = MISO1_refCh[:,1,...]

        s_MISO1_refCh = torch.unsqueeze(MISO1_refCh, dim=2)
        s_Magnitude_refCh = torch.abs(torch.sqrt(s_MISO1_refCh.real**2 + s_MISO1_refCh.imag**2)) # [B,Spks,1,T,F]
        
        with torch.no_grad():
            for shiftIdx in Mic_array[1:]:
                # print('shift Micnumber', shiftIdx)
                
                mix_stft_shift = torch.roll(mix_stft,-shiftIdx, dims=1)
                MISO1_chShift = self.model(mix_stft_shift)

                s_MISO1_chShift = torch.unsqueeze(MISO1_chShift, dim=1) #[B,1,Spks,T,F]
                s_magnitude_chShift = torch.sum(torch.abs(s_Magnitude_refCh - abs(s_MISO1_chShift)),[3,4]) #[B,Spks,Spks,T,F]
                perms = MISO1_chShift.new_tensor(list(permutations(range(self.num_spks))), dtype=torch.long)
                index_ = torch.unsqueeze(perms, dim=2)
                perms_one_hot = MISO1_chShift.new_zeros((*perms.size(), self.num_spks), dtype=torch.float).scatter_(2,index_,1)
                batchwise_distance = torch.einsum('bij,pij->bp', [s_magnitude_chShift, perms_one_hot])
                min_distance_idx = torch.argmin(batchwise_distance,dim=1)
                
                for batch_idx in range(B):              
                    align_index = torch.argmax(perms_one_hot[min_distance_idx[batch_idx]],dim=1)
                    for spk_idx in range(self.num_spks):
                        target_index = align_index[spk_idx]     
                        MISO1_stft[spk_idx][:,shiftIdx,...] = MISO1_chShift[batch_idx,target_index,...]
        

        return MISO1_stft     


    def Apply_Beamforming(self, source_stft, mix_stft, epsi=1e-6):
        """
        Input :
            mix_stft : observe STFT, size - [B, F, Ch, T], np.ndarray
            source_stft : estimated source STFT, size - [B, F, Ch, T], np.ndarray
        Output :    
            Beamform_stft : MVDR Beamforming output, size - [B, 1, T, F], torch.Tensor
        
            1. estimate target steering using EigenValue decomposition
            2. get source, noise Spatial Covariance Matrix,  S = 1/T * xx_h
            3. MVDR Beamformer
        """
        B, F, M, T = source_stft.shape

        # Apply small Diagonal matrix to prevent matrix inversion error
        eye = np.eye(M)
        eye = eye.reshape(1,1,M,M)
        delta = epsi * np.tile(eye,[B,F,1,1])

        ''' Source '''
        source_SCM = self.get_spatial_covariance_matrix(source_stft,normalize=True) # target covariance matrix, size : [B,F,C,C]
        source_SCM = 0.5 * (source_SCM + np.conj(source_SCM.swapaxes(-1,-2))) # verify hermitian symmetric  
        
        ''' Noise Spatial Covariance ''' 
        noise_signal = mix_stft - source_stft
        # s1_noise_signal = mix_stft  #MPDR
        noise_SCM = self.get_spatial_covariance_matrix(noise_signal,normalize = True) # noise covariance matrix, size : [B,F,C,C]
        # s1_SCMn = self.condition_covariance(s1_SCMn, 1e-6)
        # s1_SCMn /= np.trace(s1_SCMn, axis1=-2, axis2= -1)[...,None, None]
        noise_SCM = 0.5 * (noise_SCM + np.conj(noise_SCM.swapaxes(-1,-2))) # verify hermitian symmetric

        ''' Get Steering vector : Eigen-decomposition '''
        shape = source_SCM.shape
        source_steering = np.empty(shape[:-1], dtype=np.complex)

        # s1_SCMs += delta
        source_SCM = np.reshape(source_SCM, (-1,) + shape[-2:]) 
        eigenvals, eigenvecs = np.linalg.eigh(source_SCM)
        # Find max eigenvals
        vals = np.argmax(eigenvals, axis=-1)
        # Select eigenvec for max eigenval
        source_steering = np.array([eigenvecs[i,:,vals[i]] for i in range(eigenvals.shape[0])])
        # s1_steering = np.array([eigenvecs[i,:,vals[i]] * np.sqrt(Mic/np.linalg.norm(eigenvecs[i,:,vals[i]])) for i in range(eigenvals.shape[0])]) # [B*F,Ch,Ch]
        source_steering = np.reshape(source_steering, shape[:-1]) # [B,F,Ch]
        source_SCM = np.reshape(source_SCM, shape)
        
        ''' steering normalize with respect to the reference microphone '''
        # ver 1 
        source_steering = source_steering / np.expand_dims(source_steering[:,:,0], axis=2)
        for b_idx in range(0,B):
            for f_idx in range(0,F):
                # s1_steering[b_idx,f_idx,:] = s1_steering[b_idx,f_idx,:] / s1_steering[b_idx,f_idx,0]
                source_steering[b_idx,f_idx,:] = source_steering[b_idx,f_idx,:] * np.sqrt(M/(np.linalg.norm(source_steering[b_idx,f_idx,:])))
        
        # ver 2
        # s1_steering = self.normalize(s1_steering)

        source_steering = self.PhaseCorrection(source_steering)
        beamformer = self.get_mvdr_beamformer(source_steering, noise_SCM, delta)
        # s1_beamformer = self.blind_analytic_normalization(s1_beamformer,s1_SCMn)
        source_bf = self.apply_beamformer(beamformer,mix_stft)
        source_bf = torch.permute(torch.from_numpy(source_bf), [0,2,1])
        
        return source_bf

    def get_spatial_covariance_matrix(self,observation,normalize):
        '''
        Input : 
            observation : complex 
                            size : [B,F,C,T]
        Return :
                R       : double
                            size : [B,F,C,C]
        '''
        B,F,C,T = observation.shape
        R = np.einsum('...dt,...et-> ...de', observation, observation.conj())
        if normalize:
            normalization = np.sum(np.ones((B,F,1,T)),axis=-1, keepdims=True)
            R /= normalization
        return R
    
    def PhaseCorrection(self,W): #Matlab과 동일
        """
        Phase correction to reduce distortions due to phase inconsistencies.
        Input:
                W : steering vector
                    size : [B,F,Ch]
        """
        w = W.copy()
        B, F, Ch = w.shape
        for b_idx in range(0,B):
            for f in range(1, F):
                w[b_idx,f, :] *= np.exp(-1j*np.angle(
                    np.sum(w[b_idx,f, :] * w[b_idx,f-1, :].conj(), axis=-1, keepdims=True)))
        return w
    
    def condition_covariance(self,x,gamma):
        """see https://stt.msu.edu/users/mauryaas/Ashwini_JPEN.pdf (2.3)"""
        B,F,_,_ = x.shape
        for b_idx in range(0,B):
            scale = gamma * np.trace(x[b_idx,...]) / x[b_idx,...].shape[-1]
            scaled_eye = np.eye(x.shape[-1]) * scale
            x[b_idx,...] = (x[b_idx,...]+scaled_eye) / (1+gamma)
        return x
    
    def normalize(self,vector):
        B,F,Ch = vector.shape
        for b_idx in range(0,B):
            for ii in range(0,F):   
                weight = np.matmul(np.conjugate(vector[b_idx,ii,:]).reshape(1,-1), vector[b_idx,ii,:])
                vector[b_idx,ii,:] = (vector[b_idx,ii,:] / weight) 
        return vector     

    def blind_analytic_normalization(self,vector, noise_psd_matrix, eps=0):
        """Reduces distortions in beamformed ouptput.
            
        :param vector: Beamforming vector
            with shape (..., sensors)
        :param noise_psd_matrix:
            with shape (..., sensors, sensors)
        :return: Scaled Deamforming vector
            with shape (..., sensors)
        """
        nominator = np.einsum(
            '...a,...ab,...bc,...c->...',
            vector.conj(), noise_psd_matrix, noise_psd_matrix, vector
        )
        nominator = np.abs(np.sqrt(nominator))

        denominator = np.einsum(
            '...a,...ab,...b->...', vector.conj(), noise_psd_matrix, vector
        )
        denominator = np.abs(denominator)

        normalization = nominator / (denominator + eps)
        return vector * normalization[..., np.newaxis]


    def get_mvdr_beamformer(self, steering_vector, R_noise, delta):
        """
        Returns the MVDR beamformers vector

        Input :
            steering_vector : Acoustic transfer function vector
                                shape : [B, F, Ch]
                R_noise     : Noise spatial covariance matrix
                                shape : [B, F, Ch, Ch]
        """
        R_noise += delta
        numer = solve(R_noise, steering_vector)
        denom = np.einsum('...d,...d->...', steering_vector.conj(), numer)
        beamformer = numer / np.expand_dims(denom, axis=-1)
        return beamformer

    def apply_beamformer(self, beamformer, mixture):
        return np.einsum('...a,...at->...t',beamformer.conj(), mixture)            
